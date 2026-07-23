"""RedSignal 后端入口。
启动: uvicorn backend.main:app --reload --port 8000
客户端: http://localhost:8000/?user=u_demo_a 与 ?user=u_demo_b 两个窗口

WebSocket:
  /ws/user/{user_id}    UI 通道：状态推送、提醒、社交卡
  /ws/device/{user_id}  设备通道：真实戒指原始帧（hex）转发入口
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, confirm, gadgetbridge, mock_data, presence, zilo_protocol
from .models import (
    ButtonEventType, IMUBatch, Mode, RingButtonEvent, SessionState,
)
from .state_machine import set_mode
from .store import store
from .wearable_hub import wearable_hub

log = logging.getLogger("redsignal")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="RedSignal")
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
        notice = {
            "type": "match_notice",
            "pair_id": pair.pair_id,
            "text": "附近有一位与你互相适配的同好。双击戒指按钮，表示愿意认识。",
            "match_score": pair.match_score,
            "proximity_band": pair.proximity_band,
        }
        await hub.push(pair.user_a, notice)
        await hub.push(pair.user_b, notice)
        await broadcast_state(pair.user_a)
        await broadcast_state(pair.user_b)
        # 懒惰窗口过期检查
        asyncio.create_task(_expire_watch(pair.pair_id))


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


async def do_button_confirm(user_id: str, method: str, device_id: str) -> None:
    pair = store.active_pair_for(user_id)
    if pair is None:
        await hub.push(user_id, {"type": "no_connection", "text": "未建立连接。"})
        return
    # 窗口懒惰过期检查
    if confirm.check_window_expiry(pair.pair_id):
        await hub.push(user_id, {"type": "no_connection", "text": "未建立连接。"})
        await broadcast_state(user_id)
        return
    ev = RingButtonEvent(
        user_id=user_id, pair_id=pair.pair_id,
        event_type=ButtonEventType.DOUBLE_PRESS_CONFIRM, device_id=device_id,
    )
    result = confirm.handle_button_event(ev, confirmation_method=method)
    log.info("button %s pair=%s => %s", user_id, pair.pair_id, result.status)

    if result.status == "accepted":
        await hub.push(user_id, {"type": "self_confirmed",
                                 "text": "已记录你的确认，等待对方…"})
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
    for uid in (pair.user_a, pair.user_b):
        await hub.push(uid, {"type": "agent_content", **content})
        try:
            from .state_machine import transition
            transition(uid, SessionState.CONTENT_READY)
        except Exception:
            pass
        await broadcast_state(uid)


# ---------------- UI WebSocket ----------------
@app.websocket("/ws/user/{user_id}")
async def ws_user(ws: WebSocket, user_id: str) -> None:
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
            elif action == "mock_button":
                await do_button_confirm(user_id, "dual_ring_button", "mock")
            elif action == "app_confirm":       # App 双确认兜底
                await do_button_confirm(user_id, "app_double_confirm", "app")
            elif action == "clear_data":
                store.clear_user(user_id)
                mock_data.load()               # Demo 环境重新载入预置
                await broadcast_state(user_id)
    except WebSocketDisconnect:
        hub.user_ws.pop(user_id, None)


# ---------------- 设备 WebSocket（真实戒指帧入口） ----------------
@app.websocket("/ws/device/{user_id}")
async def ws_device(ws: WebSocket, user_id: str) -> None:
    await ws.accept()
    hub.device_ws[user_id] = ws
    wearable_hub.ring_connected(user_id)
    # 连接建立后自动下发 0x0601 开启上报
    await ws.send_json({"send_frame": zilo_protocol.build_frame(
        zilo_protocol.CMD_REPORT_START).hex()})
    try:
        while True:
            msg = await ws.receive_json()
            raw_hex = msg.get("frame")
            if not raw_hex:
                continue
            try:
                frame = zilo_protocol.parse_frame(zilo_protocol.hex_to_bytes(raw_hex))
            except zilo_protocol.FrameError as e:
                log.warning("bad frame from %s: %s", user_id, e)
                continue
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
            elif kind == "time_sync_req":
                # 戒指开机后反复发 0x0401 求时间；回 0x0402 秒级校时，它才不再刷。
                await ws.send_json({"send_frame":
                    zilo_protocol.build_time_sync_ack(int(time.time())).hex()})
            elif kind == "unknown":
                log.warning("unknown cmd 0x%04x from %s", frame.cmd, user_id)
    except WebSocketDisconnect:
        hub.device_ws.pop(user_id, None)
        wearable_hub.ring_disconnected(user_id)
        await hub.push(user_id, {"type": "device_offline",
                                 "text": "戒指连接断开，可切换 App 确认模式。"})


# ---------------- REST（调试用） ----------------
class ProfilePatch(BaseModel):
    interest_tags: list[str] | None = None
    social_goal: str | None = None


@app.get("/api/profile/{user_id}")
def get_profile(user_id: str):
    p = store.get_profile(user_id)
    if p is None:
        return {"error": "not_found"}
    return {
        "user_id": p.user_id, "nickname": p.nickname, "mode": p.mode.value,
        "interest_tags": p.interest_tags, "social_goal": p.social_goal,
        "state": store.get_state(user_id).value,
    }


@app.get("/api/ephemerals")
def list_ephemerals():
    """Demo 用：列出可扫描的匿名 ID（真实场景由 BLE 广播承载）。"""
    return [{"ephemeral_id": e, "user_id": u} for e, u in store.ephemeral_map.items()]


# ---------------- 可穿戴设备统一 API ----------------

@app.get("/api/devices/{user_id}")
def get_devices(user_id: str):
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
def update_watch(user_id: str, data: WatchHealthUpdate):
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
def sync_gadgetbridge(user_id: str, req: GadgetbridgeSyncRequest):
    """读取 Gadgetbridge 导出的 SQLite 并更新手表数据。"""
    from pathlib import Path
    health = gadgetbridge.read_db(Path(req.db_path), req.device_address)
    health.user_id = user_id
    wearable_hub.watch_sync(user_id, health)
    return wearable_hub.get(user_id).to_dict()


# ---------------- 手表 WebSocket（Android 实时转发） ----------------
@app.websocket("/ws/watch/{user_id}")
async def ws_watch(ws: WebSocket, user_id: str) -> None:
    """Android 客户端通过此通道实时转发 Gadgetbridge 广播数据。

    消息格式:
      {"type": "heart_rate", "bpm": 72}
      {"type": "steps", "count": 3456}
      {"type": "spo2", "percent": 98}
      {"type": "stress", "level": 45}
      {"type": "battery", "percent": 85}
      {"type": "sleep", "hours": 7.2}
    """
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
def extract_profile_labels(user_id: str, req: LabelExtractRequest):
    """自我介绍 → 规范化兴趣标签；若该用户有档案则写回 interest_tags。"""
    labels = agent.extract_labels(req.intro)
    prof = store.get_profile(user_id)
    if prof is not None:
        prof.interest_tags = labels
    return {"user_id": user_id, "labels": labels}


class ChatAnalyzeRequest(BaseModel):
    partner_id: str
    # messages: [{"sender": str, "ts": epoch_seconds, "text": str}]
    messages: list[dict]
    partner_tags: list[str] | None = None


@app.post("/api/chat/{user_id}/analyze")
def analyze_chat(user_id: str, req: ChatAnalyzeRequest):
    """一次聊天结束 → 评融洽度 + 算 engagement + 更新 user_id 对这类人的偏好。"""
    rapport = agent.analyze_rapport(req.messages)
    metrics = compute_engagement(req.messages, rapport=rapport["rapport"])
    # 对方标签：优先用传入的，否则查档案
    ptags = req.partner_tags
    if ptags is None:
        p = store.get_profile(req.partner_id)
        ptags = p.interest_tags if p else []
    preference.update_from_chat(user_id, ptags, metrics.engagement)
    return {
        "user_id": user_id,
        "partner_id": req.partner_id,
        "rapport": rapport,
        "metrics": metrics.to_dict(),
        "updated_preference_top": preference.top_tags(user_id),
    }


@app.get("/api/preference/{user_id}")
def get_preference(user_id: str):
    """用户学到的偏好 + 一句自然语言解释（你可能也喜欢…）。"""
    top = preference.top_tags(user_id)
    return {
        "user_id": user_id,
        "top_tags": top,
        "explanation": agent.explain_preference(top),
    }


# ---------------- 静态客户端 ----------------
@app.get("/")
def index():
    return FileResponse(CLIENT_DIR / "index.html")


app.mount("/logic", StaticFiles(directory=CLIENT_DIR / "logic"), name="logic")
