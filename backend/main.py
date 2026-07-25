"""RedSignal 后端入口。
启动: uvicorn backend.main:app --reload --port 8000
客户端: http://localhost:8000/?user=u_demo_a 与 ?user=u_demo_b 两个窗口

WebSocket:
  /ws/user/{user_id}    UI 通道：状态推送、提醒、社交卡
  /ws/device/{user_id}  设备通道：真实戒指原始帧（hex）转发入口
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from pathlib import Path

from fastapi import (
    Depends, FastAPI, File, Header, HTTPException, Request, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (agent, auth as auth_mod, chat, confirm, gadgetbridge, matching,
               mock_data, presence, transcription, zilo_protocol)
from .ring_audio import RingAudioSession
from .models import (
    ButtonEventType, IMUBatch, Mode, RingButtonEvent, SessionState,
)
from .persistence import persistence
from .state_machine import set_mode, transition
from .store import store
from .wearable_hub import wearable_hub

log = logging.getLogger("redsignal")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="RedSignal")

# 前端在 Vercel、后端在 Railway 时是跨域的：浏览器会先发 preflight，
# 不放行的话所有 fetch 都失败（WebSocket 不走 CORS，但 REST 全线挂）。
# 逗号分隔，例：REDSIGNAL_ALLOWED_ORIGINS=https://redsignal.vercel.app
_origins = [o.strip() for o in
            auth_mod._load_env().get("REDSIGNAL_ALLOWED_ORIGINS", "").split(",")
            if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    log.info("CORS 放行来源: %s", _origins)

mock_data.load()

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"


# ---------------- WebSocket hub ----------------
class Hub:
    def __init__(self) -> None:
        self.user_ws: dict[str, WebSocket] = {}
        self.device_ws: dict[str, WebSocket] = {}

    async def push(self, user_id: str, message: dict) -> None:
        ws = self.user_ws.get(user_id)
        if ws is not None:
            try:
                await ws.send_json(message)
            except Exception:
                self.user_ws.pop(user_id, None)

    async def push_device(self, user_id: str, message: dict) -> None:
        ws = self.device_ws.get(user_id)
        if ws is not None:
            try:
                await ws.send_json(message)
            except Exception:
                self.device_ws.pop(user_id, None)


hub = Hub()

# 演示自动播放：3 秒后自动配对 + 自动替双方"双击确认"。
# 现场网络差/人手不够时是救命的，但它会**掩盖真实链路的故障**——
# 拿真戒指双击时，就算蓝牙那一跳压根没通，2 秒后它也会自己确认成功，
# 于是你以为戒指通了，其实完全没有。验真硬件时必须设 0。
DEMO_AUTOPLAY = auth_mod._load_env().get(
    "REDSIGNAL_DEMO_AUTOPLAY", "1") not in ("0", "false", "False")
if DEMO_AUTOPLAY:
    log.warning("演示自动播放已开启：会自动配对并替双方完成确认。"
                "验证真实戒指前请设 REDSIGNAL_DEMO_AUTOPLAY=0，否则测不出真假。")
else:
    log.info("演示自动播放已关闭：匹配与确认全部走真实链路")

_ring_audio_sessions: dict[str, RingAudioSession] = {}
_device_rx_buffers: dict[str, bytearray] = {}
_demo_auto_confirm_tasks: dict[str, asyncio.Task] = {}


def _feed_device_frames(user_id: str, chunk: bytes) -> list[zilo_protocol.ZiloFrame]:
    """把 Web Bluetooth 的任意分片重新组装成协议帧。"""
    buf = _device_rx_buffers.setdefault(user_id, bytearray())
    buf.extend(chunk)
    result: list[zilo_protocol.ZiloFrame] = []
    while True:
        if len(buf) < zilo_protocol.HEADER_LEN:
            break
        if buf[0] != zilo_protocol.FRAME_MAGIC:
            try:
                del buf[:buf.index(zilo_protocol.FRAME_MAGIC)]
            except ValueError:
                buf.clear()
                break
        if len(buf) < zilo_protocol.HEADER_LEN:
            break
        body_len = int.from_bytes(buf[5:9], "big")
        total = zilo_protocol.HEADER_LEN + body_len
        if body_len > 2 * 1024 * 1024:
            del buf[0]
            continue
        if len(buf) < total:
            break
        raw = bytes(buf[:total])
        del buf[:total]
        try:
            result.append(zilo_protocol.parse_frame(raw))
        except zilo_protocol.FrameError:
            # 丢掉一个字节后继续寻找下一个 magic，避免坏帧卡死整个连接。
            continue
    return result


@app.on_event("shutdown")
async def _flush_persistence() -> None:
    """关服前把写回队列排空，避免最后几条 encounter 丢失。"""
    await persistence.flush()


async def broadcast_state(user_id: str) -> None:
    await hub.push(user_id, {
        "type": "state",
        "state": store.get_state(user_id).value,
        "mode": (store.get_profile(user_id).mode.value
                 if store.get_profile(user_id) else "off"),
    })


# ---------------- 业务动作（Mock 与真实事件共用） ----------------
async def do_set_mode(user_id: str, mode: str) -> None:
    set_mode(user_id, Mode(mode))
    await broadcast_state(user_id)
    # 切蓝会取消候选：通知对端
    pair = store.active_pair_for(user_id)
    if mode == "off" and pair is None:
        pass
    for uid in list(hub.user_ws):
        if uid != user_id:
            await broadcast_state(uid)


async def do_sighting(observer: str, ephemeral_id: str, rssi: int) -> None:
    pair, reason = presence.report_sighting(observer, ephemeral_id, rssi)
    log.info("sighting %s->%s rssi=%s => %s", observer, ephemeral_id, rssi, reason)
    await hub.push(observer, {"type": "sighting_ack", "reason": reason})
    if pair is not None:
        await publish_match(pair)


async def publish_match(pair) -> None:
    notice = {
        "type": "match_notice",
        "pair_id": pair.pair_id,
        "text": "附近有一位与你互相适配的同好。按下戒指，表示愿意认识。",
        "match_score": pair.match_score,
        "proximity_band": pair.proximity_band,
    }
    await hub.push(pair.user_a, notice)
    await hub.push(pair.user_b, notice)
    await broadcast_state(pair.user_a)
    await broadcast_state(pair.user_b)
    asyncio.create_task(_expire_watch(pair.pair_id))


async def _demo_auto_confirm(pair) -> None:
    """Demo-only: simulate both double presses, then pause before connecting."""
    # Leave a visible beat after the match card before the confirmation state.
    await asyncio.sleep(2)
    if store.active_pair_for(pair.user_a) is not pair:
        return
    await do_button_confirm(pair.user_a, "demo_auto", "demo", demo_pause=False)
    if store.active_pair_for(pair.user_b) is not pair:
        return
    transition(pair.user_b, SessionState.SELF_CONFIRMED)
    await hub.push(pair.user_b, {
        "type": "self_confirmed",
        "text": "已确认，正在建立连接…",
    })
    await broadcast_state(pair.user_b)
    await asyncio.sleep(2)
    if store.active_pair_for(pair.user_b) is pair:
        await do_button_confirm(pair.user_b, "demo_auto", "demo", demo_pause=False)


def schedule_demo_auto_confirm(pair) -> None:
    if not DEMO_AUTOPLAY:
        return                    # 关掉自动播放时，确认只能来自真戒指或 App 按钮
    task = _demo_auto_confirm_tasks.get(pair.pair_id)
    if task is None or task.done():
        _demo_auto_confirm_tasks[pair.pair_id] = asyncio.create_task(
            _demo_auto_confirm(pair)
        )


async def do_demo_match(user_id: str) -> None:
    """现场演示快捷入口：仍走 presence/matching/通知全链路。"""
    if not DEMO_AUTOPLAY:
        log.info("demo_match 被忽略（REDSIGNAL_DEMO_AUTOPLAY=0）")
        return
    other = "u_demo_b" if user_id == "u_demo_a" else "u_demo_a"
    if store.get_profile(other) is None:
        return
    await do_set_mode(user_id, Mode.FRIEND)
    await do_set_mode(other, Mode.FRIEND)
    # demo 仍使用正式 pair/通知链路，但不让现场预置资料的 60 分阈值挡住演示。
    existing = store.active_pair_for(user_id)
    if existing is not None:
        await publish_match(existing)
        schedule_demo_auto_confirm(existing)
        return
    me = store.get_profile(user_id)
    them = store.get_profile(other)
    score = matching.compat_score(me, them)
    candidate = matching.Candidate(
        user_id=other, compat_score=max(score, 80), rank_score=max(score, 80),
        dwell_seconds=2.0, proximity_band="very_near",
        breakdown=matching.score_breakdown(me, them),
    )
    pair = matching.create_pair(user_id, candidate)
    for uid in (pair.user_a, pair.user_b):
        if store.get_state(uid) == SessionState.DISCOVERABLE:
            transition(uid, SessionState.CANDIDATE_NEARBY)
        transition(uid, SessionState.NOTIFIED)
    await publish_match(pair)
    schedule_demo_auto_confirm(pair)


async def _expire_watch(pair_id: str) -> None:
    from . import config
    await asyncio.sleep(config.CANDIDATE_TTL_SECONDS + 1)
    if confirm.check_window_expiry(pair_id):
        pair = store.get_pair(pair_id)
        if pair:
            for uid in (pair.user_a, pair.user_b):
                await hub.push(uid, {"type": "no_connection",
                                     "text": "未建立连接。"})
                await broadcast_state(uid)


async def do_button_confirm(
    user_id: str, method: str, device_id: str, *, demo_pause: bool = True
) -> None:
    pair = store.active_pair_for(user_id)
    if pair is None:
        await hub.push(user_id, {"type": "no_connection", "text": "未建立连接。"})
        return
    # 窗口懒惰过期检查
    if confirm.check_window_expiry(pair.pair_id):
        await hub.push(user_id, {"type": "no_connection", "text": "未建立连接。"})
        await broadcast_state(user_id)
        return
    # Demo hardware flow intentionally leaves a two-second confirmation pause
    # after the physical double press, so the action is visible in the demo.
    if demo_pause and user_id in {"u_demo_a", "u_demo_b"}:
        await asyncio.sleep(2)
    ev = RingButtonEvent(
        user_id=user_id, pair_id=pair.pair_id,
        event_type=ButtonEventType.DOUBLE_PRESS_CONFIRM, device_id=device_id,
    )
    result = confirm.handle_button_event(ev, confirmation_method=method)
    log.info("button %s pair=%s => %s", user_id, pair.pair_id, result.status)

    if result.status == "accepted":
        await hub.push(user_id, {"type": "self_confirmed",
                                 "text": "已确认，正在建立连接…"})
        await broadcast_state(user_id)
    elif result.status == "encounter_created":
        enc = result.encounter
        assert enc is not None
        # 先推送社交卡（Agent 失败不影响交换，PRD 12.2）
        for uid in (pair.user_a, pair.user_b):
            await hub.push(uid, {
                "type": "encounter",
                "encounter_id": enc.encounter_id,
                "card": enc.shared_fields[uid],
                "confirmation_method": enc.confirmation_method,
            })
            await broadcast_state(uid)
        asyncio.create_task(_generate_agent_content(enc.encounter_id))
    elif result.status == "pair_dead":
        await hub.push(user_id, {"type": "no_connection", "text": "未建立连接。"})


async def do_chat_send(user_id: str, encounter_id: str, text: str) -> None:
    """把一条消息投递给 encounter 的另一方。

    发送方不回声——它已经乐观渲染过了；重连时用 /api/chat/.../history 对齐。
    """
    try:
        msg = chat.chat_store.append(encounter_id, user_id, text)
        partner = chat.chat_store.partner_of(encounter_id, user_id)
    except chat.ChatError as e:
        log.warning("chat_send rejected %s: %s", user_id, e)
        await hub.push(user_id, {"type": "chat_error", "text": "消息发送失败。"})
        return
    await hub.push(user_id, {"type": "chat_sent",
                             "encounter_id": encounter_id,
                             "message_id": msg.message_id})
    await hub.push(partner, chat.chat_store.as_payload(msg, partner))


async def _generate_agent_content(encounter_id: str) -> None:
    enc = store.encounters.get(encounter_id)
    if enc is None:
        return
    pair = store.get_pair(enc.pair_id)
    a = store.get_profile(pair.user_a)
    b = store.get_profile(pair.user_b)
    shared = sorted(set(a.interest_tags) & set(b.interest_tags))
    payload = agent.build_payload(a.event_id, a.mode.value, shared,
                                  a.social_goal, b.social_goal,
                                  enc.confirmation_method)
    content = await asyncio.to_thread(agent.generate, payload)
    enc.agent_content = content
    store.set_agent_content(encounter_id, content)
    for uid in (pair.user_a, pair.user_b):
        await hub.push(uid, {"type": "agent_content", **content})
        try:
            from .state_machine import transition
            transition(uid, SessionState.CONTENT_READY)
        except Exception:
            pass
        await broadcast_state(uid)


# 匿名昵称词库。风格对齐 mock_data 里的「信号狐 / 夜航鲸 / 折射 / 缓存」。
_NICK_A = ["夜航", "信号", "低频", "回声", "折射", "南极", "热带", "跳电", "缓存",
           "浮标", "北纬", "潮汐", "候鸟", "石英", "银盐", "长波", "微光", "晚风"]
_NICK_B = ["鲸", "狐", "鹿", "鸦", "鲤", "隼", "獭", "雀", "豹", "鹤", "蜂", "鲨"]


def generate_nickname(user_id: str) -> str:
    """给新用户一个匿名昵称。

    绝不能拿邮箱前缀当昵称——社交卡是要给陌生人看的，
    zhangsan@gmail.com 会直接变成「zhangsan」，等于把邮箱的一半交出去。
    这跟 models.FORBIDDEN_FIELDS 想防的是同一件事。

    用 user_id 做种子，同一个人每次重建档案都得到同一个名字，不会跳来跳去。
    """
    h = int(hashlib.sha256(user_id.encode()).hexdigest()[:8], 16)
    return _NICK_A[h % len(_NICK_A)] + _NICK_B[(h // len(_NICK_A)) % len(_NICK_B)]


def ensure_profile(user_id: str, token: str = "") -> None:
    """首次登录的真实用户还没有档案，这里补一个空壳。

    昵称先用邮箱前缀顶上，标签/想找留空——用户进 App 后在「我的标签」
    和「今天想找」里自己填，走的是已有的 PATCH /api/profile 路径。
    标签为空时匹配算不出兴趣重合分，自然进不了候选，不会打扰别人。
    """
    if store.get_profile(user_id) is not None:
        return
    from . import config as cfg
    from .models import UserEventProfile
    nickname = generate_nickname(user_id)
    store.upsert_profile(UserEventProfile(
        user_id=user_id, event_id=cfg.DEFAULT_EVENT_ID, mode=Mode.OFF,
        social_goal="project_teammate", interest_tags=[],
        communication_style="deep_small_group", share_bundle={}, nickname=nickname,
    ))
    log.info("新用户建档: %s (%s)", user_id, nickname)


def ensure_ephemeral(user_id: str) -> str:
    """保证该用户有一个可被扫描到的匿名编号，返回它。

    真实产品里这个编号每几分钟轮换一次（models.RollingPresence 的设计），
    这里先一人一个稳定值——轮换要连着 BLE 广播一起做，不是后端单方面能定的。
    """
    for eph, uid in store.ephemeral_map.items():
        if uid == user_id:
            return eph
    eph = f"eph_{uuid.uuid4().hex[:12]}"
    store.register_ephemeral(eph, user_id)
    return eph


@app.get("/api/auth/config")
def auth_config():
    """前端拿 Supabase 地址与 anon key。

    anon key 设计上就是公开的（每个客户端都要带），与 service_role 完全不同：
    它受 RLS 约束，而本项目所有表都 enable RLS 且不建 anon policy，
    所以就算泄露也读不到任何业务数据——前端只用它调 Auth 接口。
    """
    env = auth_mod._load_env()
    return {
        "supabase_url": env.get("SUPABASE_URL", ""),
        "anon_key": env.get("SUPABASE_ANON_KEY", ""),
        "demo_mode": auth_mod.auth.demo_mode,
        "demo_autoplay": DEMO_AUTOPLAY,     # 前端据此决定要不要发 demo_match
        "configured": bool(env.get("SUPABASE_URL") and env.get("SUPABASE_ANON_KEY")),
    }


# ---------------- UI WebSocket ----------------
@app.websocket("/ws/user/{user_id}")
async def ws_user(ws: WebSocket, user_id: str, token: str = "") -> None:
    """URL 里的 user_id 只是「前端声称的身份」，一律以 token 的 sub 为准。

    没有这一步，任何人把地址栏改成别人的 id 就能收到对方的匹配提醒与聊天。
    """
    try:
        real_id = auth_mod.auth.resolve(token, user_id)
    except auth_mod.AuthError as e:
        log.warning("ws_user 鉴权失败: %s", e)
        await ws.close(code=4401)          # 4401 = 未认证，前端据此跳登录页
        return
    if real_id != user_id:
        # 静默改绑成 token 的身份是"安全但迷惑"的：不会泄露数据，
        # 但会把客户端 bug 藏起来，而且与 REST 的 403 行为不一致。宁可显式拒绝。
        log.warning("ws_user 身份不符: URL 声称 %s，token 是 %s", user_id, real_id)
        await ws.close(code=4403)
        return
    ensure_profile(user_id, token)
    # 每次连接都确保有匿名编号（幂等）：没有它就等于不存在——别人扫不到你
    # （不进 /api/ephemerals），你被上报时 resolve_ephemeral 也返回 None。
    # 放在这里而不是 ensure_profile 里，是为了覆盖修复前已建档的用户。
    ensure_ephemeral(user_id)
    await ws.accept()
    hub.user_ws[user_id] = ws
    await broadcast_state(user_id)
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            if action == "set_mode":
                await do_set_mode(user_id, msg["mode"])
            elif action == "sighting":
                await do_sighting(user_id, msg["ephemeral_id"], int(msg.get("rssi", -60)))
            elif action == "demo_match" and user_id in {"u_demo_a", "u_demo_b"}:
                await do_demo_match(user_id)
            elif action == "mock_button":
                await do_button_confirm(user_id, "dual_ring_button", "mock")
            elif action == "app_confirm":       # App 双确认兜底
                await do_button_confirm(user_id, "app_double_confirm", "app")
            elif action == "chat_send":
                await do_chat_send(user_id, msg.get("encounter_id", ""),
                                   msg.get("text", ""))
            elif action == "clear_data":
                store.clear_user(user_id)
                chat.chat_store.clear_user(user_id)
                mock_data.load()               # Demo 环境重新载入预置
                await broadcast_state(user_id)
    except WebSocketDisconnect:
        hub.user_ws.pop(user_id, None)


# ---------------- 设备 WebSocket（真实戒指帧入口） ----------------
@app.websocket("/ws/device/{user_id}")
async def ws_device(ws: WebSocket, user_id: str, token: str = "") -> None:
    """与 /ws/user 同样的身份校验——这条通道比 UI 通道更敏感。

    戒指的「按钮双击」帧（0x0703）收到后直接调 do_button_confirm，
    不校验身份的话，任何人连到别人的 device 通道发一帧，就能替对方完成确认，
    绕过「双方各自双击」这条产品红线。
    """
    try:
        real_id = auth_mod.auth.resolve(token, user_id)
    except auth_mod.AuthError as e:
        log.warning("ws_device 鉴权失败: %s", e)
        await ws.close(code=4401)
        return
    if real_id != user_id:
        log.warning("ws_device 身份不符: URL 声称 %s，token 是 %s", user_id, real_id)
        await ws.close(code=4403)
        return
    await ws.accept()
    hub.device_ws[user_id] = ws
    wearable_hub.ring_connected(user_id)
    async def send_ring_frame(raw: bytes) -> None:
        await ws.send_json({"send_frame": raw.hex()})

    async def notify_ring_audio(event: dict) -> None:
        log.info("ring audio user=%s %s", user_id, event)
        await hub.push(user_id, event)

    async def transcribe_ring_audio(path: Path, metadata: dict) -> None:
        text = await transcription.transcribe_file(path)
        profile = store.get_profile(user_id)
        if profile is None:
            return
        profile.share_bundle["team_need"] = text
        store.upsert_profile(profile)
        await hub.push(user_id, {"type": "ring_audio", "stage": "transcribed",
                                 "fileIndex": metadata.get("fileIndex"), "text": text})

    audio_session = RingAudioSession(user_id, send_ring_frame, notify_ring_audio,
                                     transcribe_ring_audio)
    _ring_audio_sessions[user_id] = audio_session
    _device_rx_buffers[user_id] = bytearray()
    # 连接建立后：先问系统信息（电量/固件/型号），再开启六轴上报
    await send_ring_frame(zilo_protocol.build_frame(zilo_protocol.CMD_SYS_INFO_REQ))
    await send_ring_frame(zilo_protocol.build_frame(zilo_protocol.CMD_REPORT_START))
    # 建立录音数量基线；之后每次轮询发现 count 增长就自动提取新文件。
    await audio_session.request_list()
    async def poll_recordings() -> None:
        while True:
            await asyncio.sleep(5)
            await audio_session.request_list()
    poll_task = asyncio.create_task(poll_recordings())
    try:
        while True:
            msg = await ws.receive_json()
            raw_hex = msg.get("frame")
            if not raw_hex:
                continue
            for frame in _feed_device_frames(user_id, zilo_protocol.hex_to_bytes(raw_hex)):
                await audio_session.handle_frame(frame)
                kind = zilo_protocol.classify(frame)
                if kind == "double_press_confirm":
                    wearable_hub.ring_button_press(user_id)
                    await do_button_confirm(user_id, "dual_ring_button", f"zilo_{user_id}")
                elif kind == "imu_batch":
                    parsed = zilo_protocol.parse_imu_body(frame.body)
                    if parsed:
                        store.add_imu(IMUBatch(user_id, parsed.seq_start, parsed.seq_end,
                                               parsed.uptime_ms, parsed.accel, parsed.gyro))
                        wearable_hub.ring_imu(user_id, parsed.accel, parsed.gyro)
                elif kind == "motion_gesture":
                    gesture_id = frame.body[4] if len(frame.body) >= 5 else 0
                    gesture_names = {0: "idle", 1: "rotate_back", 2: "rotate_front", 3: "wave"}
                    wearable_hub.ring_gesture(user_id, gesture_names.get(gesture_id, "unknown"))
                elif kind == "sys_info":
                    # 0x0102 系统信息：解析电量/固件/型号 → 更新融合中心（前端仪表盘展示）
                    info = zilo_protocol.parse_sys_info(frame.body)
                    wearable_hub.ring_connected(
                        user_id,
                        firmware=info.get("firmwareVersion", ""),
                        battery=info.get("batteryPercent", -1),
                        model=info.get("model", "ring_sound"),
                    )
                elif kind == "time_sync_req":
                    # 戒指开机后反复发 0x0401 求时间；回 0x0402 秒级校时，它才不再刷。
                    await send_ring_frame(zilo_protocol.build_time_sync_ack(int(time.time())))
                elif kind == "unknown":
                    log.warning("unknown cmd 0x%04x from %s", frame.cmd, user_id)
    except WebSocketDisconnect:
        poll_task.cancel()
        hub.device_ws.pop(user_id, None)
        _ring_audio_sessions.pop(user_id, None)
        _device_rx_buffers.pop(user_id, None)
        wearable_hub.ring_disconnected(user_id)
        await hub.push(user_id, {"type": "device_offline",
                                 "text": "戒指连接断开，可切换 App 确认模式。"})


# ---------------- REST（调试用） ----------------
def require_user(user_id: str, authorization: str = Header(default="")) -> str:
    """路径里的 user_id 必须与 token 的 sub 一致，否则 403。

    演示模式下预置 mock 用户可无 token 通过（见 auth.Auth.resolve）。
    """
    try:
        real = auth_mod.auth.resolve(authorization, user_id)
    except auth_mod.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    if real != user_id:
        raise HTTPException(status_code=403, detail="user_id mismatch")
    return real


CallerIsUser = Depends(require_user)


class ProfilePatch(BaseModel):
    interest_tags: list[str] | None = None
    social_goal: str | None = None
    wish: str | None = None            # "今天想找" 自由文本，落在 share_bundle.team_need


def _profile_dict(user_id: str, p) -> dict:
    return {
        "user_id": p.user_id, "nickname": p.nickname, "mode": p.mode.value,
        "interest_tags": p.interest_tags, "social_goal": p.social_goal,
        "wish": p.share_bundle.get("team_need", ""),
        "state": store.get_state(user_id).value,
    }


@app.get("/api/profile/{user_id}")
def get_profile(user_id: str, _: str = CallerIsUser):
    p = store.get_profile(user_id)
    if p is None:
        return {"error": "not_found"}
    return _profile_dict(user_id, p)


@app.patch("/api/profile/{user_id}")
def patch_profile(user_id: str, req: ProfilePatch, _: str = CallerIsUser):
    """App 端编辑标签 / 社交目标 / "今天想找"。写内存 + 触发 Supabase 写回。"""
    p = store.get_profile(user_id)
    if p is None:
        return {"error": "not_found"}
    if req.interest_tags is not None:
        p.interest_tags = req.interest_tags
    if req.social_goal is not None:
        p.social_goal = req.social_goal
    if req.wish is not None:
        p.share_bundle["team_need"] = req.wish
    store.upsert_profile(p)            # 复用 upsert 的持久化路径
    return _profile_dict(user_id, p)


@app.get("/api/ephemerals")
def list_ephemerals():
    """Demo 用：列出可扫描的匿名 ID（真实场景由 BLE 广播承载）。"""
    return [{"ephemeral_id": e, "user_id": u} for e, u in store.ephemeral_map.items()]


# ---------------- 可穿戴设备统一 API ----------------

@app.get("/api/devices/{user_id}")
def get_devices(user_id: str, _: str = CallerIsUser):
    """获取用户所有可穿戴设备的统一快照（Ring + Watch 合并）。"""
    return wearable_hub.get(user_id).to_dict()


class WatchHealthUpdate(BaseModel):
    """Android 端转发的实时手表数据（Gadgetbridge broadcast → 我们的 App → 后端）。"""
    heart_rate: int | None = None
    steps: int | None = None
    spo2: int | None = None
    stress: int | None = None
    battery: int | None = None


@app.post("/api/devices/{user_id}/watch")
def update_watch(user_id: str, data: WatchHealthUpdate, _: str = CallerIsUser):
    """接收 Android 客户端转发的小米手表实时数据。"""
    if data.heart_rate is not None:
        wearable_hub.watch_realtime_hr(user_id, data.heart_rate)
    if data.steps is not None:
        wearable_hub.watch_realtime_steps(user_id, data.steps)
    snap = wearable_hub.get(user_id)
    if data.spo2 is not None:
        snap.watch.spo2_percent = data.spo2
    if data.stress is not None:
        snap.watch.stress_level = data.stress
    if data.battery is not None:
        snap.watch.battery_percent = data.battery
    snap.watch.connected = True
    return {"ok": True}


class GadgetbridgeSyncRequest(BaseModel):
    """指定 Gadgetbridge 导出 DB 路径，触发同步。"""
    db_path: str
    device_address: str | None = None


@app.post("/api/devices/{user_id}/gadgetbridge-sync")
def sync_gadgetbridge(user_id: str, req: GadgetbridgeSyncRequest,
                      _: str = CallerIsUser):
    """读取 Gadgetbridge 导出的 SQLite 并更新+持久化手表数据。"""
    from pathlib import Path
    from . import watch_store
    p = Path(req.db_path)
    health = gadgetbridge.read_db(p, req.device_address)
    health.user_id = user_id
    wearable_hub.watch_sync(user_id, health)
    stats = watch_store.store_snapshot(user_id, health)   # 持久化时序
    if p.exists():
        watch_store.store_raw(user_id, p)                 # 原始库留档
    snap = wearable_hub.get(user_id).to_dict()
    snap["stored"] = stats
    return snap


# ---------------- 手表 WebSocket（Android 实时转发） ----------------
@app.websocket("/ws/watch/{user_id}")
async def ws_watch(ws: WebSocket, user_id: str, token: str = "") -> None:
    """Android 客户端通过此通道实时转发 Gadgetbridge 广播数据。

    消息格式:
      {"type": "heart_rate", "bpm": 72}
      {"type": "steps", "count": 3456}
      {"type": "spo2", "percent": 98}
      {"type": "stress", "level": 45}
      {"type": "battery", "percent": 85}
      {"type": "sleep", "hours": 7.2}
    """
    # 生理数据只做个人展示、不进匹配，但也不该让别人往你账号里灌假数据
    try:
        real_id = auth_mod.auth.resolve(token, user_id)
    except auth_mod.AuthError as e:
        log.warning("ws_watch 鉴权失败: %s", e)
        await ws.close(code=4401)
        return
    if real_id != user_id:
        await ws.close(code=4403)
        return
    await ws.accept()
    wearable_hub.get(user_id).watch.connected = True
    log.info("watch ws connected: %s", user_id)
    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")
            if msg_type == "heart_rate":
                wearable_hub.watch_realtime_hr(user_id, int(msg["bpm"]))
                # 推送给前端
                await hub.push(user_id, {
                    "type": "watch_update",
                    "data": {"heart_rate_bpm": msg["bpm"]},
                })
            elif msg_type == "steps":
                wearable_hub.watch_realtime_steps(user_id, int(msg["count"]))
                await hub.push(user_id, {
                    "type": "watch_update",
                    "data": {"today_steps": msg["count"]},
                })
            elif msg_type == "spo2":
                snap = wearable_hub.get(user_id)
                snap.watch.spo2_percent = int(msg["percent"])
            elif msg_type == "stress":
                snap = wearable_hub.get(user_id)
                snap.watch.stress_level = int(msg["level"])
            elif msg_type == "battery":
                snap = wearable_hub.get(user_id)
                snap.watch.battery_percent = int(msg["percent"])
            elif msg_type == "sleep":
                snap = wearable_hub.get(user_id)
                snap.watch.sleep_hours = float(msg["hours"])
    except WebSocketDisconnect:
        wearable_hub.watch_disconnected(user_id)
        log.info("watch ws disconnected: %s", user_id)


# ---------------- Agent + 偏好学习 API ----------------
from .preference import preference, compute_engagement  # noqa: E402


class LabelExtractRequest(BaseModel):
    intro: str


@app.post("/api/profile/{user_id}/labels")
def extract_profile_labels(user_id: str, req: LabelExtractRequest,
                           _: str = CallerIsUser):
    """自我介绍 → 规范化兴趣标签；若该用户有档案则写回 interest_tags。"""
    labels = agent.extract_labels(req.intro)
    prof = store.get_profile(user_id)
    if prof is not None:
        prof.interest_tags = labels
    return {"user_id": user_id, "labels": labels}


@app.get("/api/chat/{user_id}/history/{encounter_id}")
def chat_history(user_id: str, encounter_id: str, _: str = CallerIsUser):
    """会话历史。重连/刷新后用它对齐，mine 由后端判定。"""
    try:
        chat.chat_store.partner_of(encounter_id, user_id)   # 参与者校验
    except chat.ChatError:
        return {"error": "forbidden", "messages": []}
    return {"encounter_id": encounter_id,
            "messages": [chat.chat_store.as_payload(m, user_id)
                         for m in chat.chat_store.history(encounter_id)]}


class ChatAnalyzeRequest(BaseModel):
    """两种用法：
    - 传 encounter_id：后端自己取会话记录与对方标签（App 走这条）；
    - 传 partner_id + messages：调用方自带数据（工具/测试走这条）。
    """
    encounter_id: str | None = None
    partner_id: str | None = None
    # messages: [{"sender": str, "ts": epoch_seconds, "text": str}]
    messages: list[dict] | None = None
    partner_tags: list[str] | None = None


@app.post("/api/chat/{user_id}/analyze")
def analyze_chat(user_id: str, req: ChatAnalyzeRequest, _: str = CallerIsUser):
    """一次聊天结束 → 评融洽度 + 算 engagement + 更新 user_id 对这类人的偏好。"""
    partner_id, messages = req.partner_id, req.messages
    if req.encounter_id:
        try:
            partner_id = chat.chat_store.partner_of(req.encounter_id, user_id)
        except chat.ChatError:
            return {"error": "forbidden"}
        messages = chat.chat_store.analyze_payload(req.encounter_id)
    if partner_id is None or messages is None:
        return {"error": "need encounter_id or (partner_id + messages)"}
    if not messages:
        return {"error": "no_messages", "user_id": user_id, "partner_id": partner_id}

    rapport = agent.analyze_rapport(messages)
    metrics = compute_engagement(messages, rapport=rapport["rapport"])
    # 对方标签：优先用传入的，否则查档案
    ptags = req.partner_tags
    if ptags is None:
        p = store.get_profile(partner_id)
        ptags = p.interest_tags if p else []
    preference.update_from_chat(user_id, ptags, metrics.engagement)
    return {
        "user_id": user_id,
        "partner_id": partner_id,
        "rapport": rapport,
        "metrics": metrics.to_dict(),
        "updated_preference_top": preference.top_tags(user_id),
    }


@app.get("/api/preference/{user_id}")
def get_preference(user_id: str, _: str = CallerIsUser):
    """用户学到的偏好 + 一句自然语言解释（你可能也喜欢…）。"""
    top = preference.top_tags(user_id)
    return {
        "user_id": user_id,
        "top_tags": top,
        "explanation": agent.explain_preference(top),
    }


@app.get("/api/watch/{user_id}/dump")
def watch_dump(user_id: str, limit: int = 500, _: str = CallerIsUser):
    """导出已持久化的全部手表数据：计数 + 最近样本 + 最新快照 + 原始库列表。"""
    from . import watch_store
    return watch_store.dump(user_id, limit=limit)


@app.get("/api/watch/{user_id}/raw")
def watch_raw_latest(user_id: str, _: str = CallerIsUser):
    """下载该用户最近一次留档的原始 Gadgetbridge 库（全部数据）。"""
    from . import watch_store
    d = watch_store.dump(user_id, limit=1)
    if not d["raw_uploads"]:
        return {"error": "no raw upload stored yet"}
    return FileResponse(d["raw_uploads"][0]["path"],
                        filename=f"gadgetbridge_{user_id}.sqlite")


@app.post("/api/devices/{user_id}/gadgetbridge-upload")
async def gadgetbridge_upload(user_id: str, file: UploadFile = File(...),
                              _: str = CallerIsUser):
    """网页版手表接入：手机上从 Gadgetbridge 导出 SQLite，在网页里直接上传本文件。

    后端把上传的库落到临时文件 → gadgetbridge.read_db 解析 → 更新 wearable_hub。
    全程只用网页，无需 adb/电脑/Termux。
    """
    import tempfile
    from pathlib import Path
    from . import watch_store
    data = await file.read()
    tmp = Path(tempfile.gettempdir()) / f"gb_upload_{user_id}.sqlite"
    tmp.write_bytes(data)
    health = gadgetbridge.read_db(tmp)
    health.user_id = user_id
    wearable_hub.watch_sync(user_id, health)
    stats = watch_store.store_snapshot(user_id, health)   # 持久化全部时序
    raw_path = watch_store.store_raw(user_id, tmp)         # 原始库整份留档
    snap = wearable_hub.get(user_id).to_dict()
    return {"ok": True, "bytes": len(data), "watch": snap["watch"],
            "stored": stats, "raw_saved": raw_path}


class IcebreakRequest(BaseModel):
    phones: list[str]                         # 双方手机号，如 ["+1555...", "+1666..."]
    shared_interests: list[str] = []
    event: str = "AdventureX 2026"
    mode: str = "friend"


@app.post("/api/demo/{user_id}/icebreak")
def demo_icebreak(user_id: str, req: IcebreakRequest, _: str = CallerIsUser):
    """破冰官演示：匹配确认后，让有手机号的 Agent 主动给双方发 iMessage。

    评委用自己手机号即可现场体验：戴戒指/触发匹配 → 手机收到破冰官消息。
    photon-agent 未启动时返回 delivered=false（静默降级，不报错）。
    """
    from . import photon
    text = photon.build_icebreaker_text(req.event, req.mode, req.shared_interests)
    delivered = photon.send_icebreak(req.phones, text, group=True)
    return {"delivered": delivered, "recipients": req.phones, "text": text}


@app.post("/api/demo/{user_id}/mock")
def demo_mock(user_id: str, _: str = CallerIsUser):
    """演示用：无硬件/无安卓时，注入一组戒指+手表数据，让仪表盘展示全链路。"""
    wearable_hub.ring_connected(user_id, firmware="V2.000.0001.0015",
                                battery=96, model="ring_sound")
    wearable_hub.ring_gesture(user_id, "wave")
    wearable_hub.ring_button_press(user_id)
    wearable_hub.ring_imu(user_id, (128.0, -64.0, 992.0), (12.0, -8.0, 3.0))
    wearable_hub.watch_realtime_hr(user_id, 74)
    wearable_hub.watch_realtime_steps(user_id, 8213)
    snap = wearable_hub.get(user_id)
    snap.watch.connected = True
    snap.watch.spo2_percent = 98
    snap.watch.stress_level = 32
    snap.watch.sleep_hours = 7.4
    snap.watch.battery_percent = 81
    return wearable_hub.get(user_id).to_dict()


# ---------------- 静态客户端 ----------------
@app.get("/")
def index():
    return FileResponse(CLIENT_DIR / "index.html")


@app.get("/dashboard")
def dashboard():
    """设备融合演示仪表盘：Ring + 小米手表全数据 + 学习偏好。"""
    return FileResponse(CLIENT_DIR / "dashboard.html")


@app.get("/app")
def mobile_app(request: Request):
    """手机端主应用入口。重定向到带尾斜杠的挂载点，
    否则 index.html 里的相对路径（logic/ Anims/ UIstatic/）会解析到站点根目录。"""
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/app/{q}")


app.mount("/logic", StaticFiles(directory=CLIENT_DIR / "logic"), name="logic")
# /app/ 下的静态资源（Anims 视频、UIstatic 图、status 子页、backend.js）
app.mount("/app", StaticFiles(directory=CLIENT_DIR / "app", html=True), name="app")
