"""实测戒指录音：列表 → 提取 → 数据帧。

第一轮实测得到的事实（2026-07-25）：
  - 戒指睡着时**完全不广播**，必须按按钮唤醒；广播窗口只有几秒，
    "扫完再挑再连"必然超时 —— 所以下面一发现就立刻连。
  - `0x0501` **有响应**：回 `0x0502`，body = 00000000003f
    按 err(u32)=0 + u16=0x003f 解，很可能表示**已有 63 条录音**。
  - 第一轮里单击/长按/双击全部收不到事件 —— 那是探测工具的 bug：
    **忘了发 0x0601 开启上报**。ring_session.py 当年能收 27 次 0x0703
    正是因为它发了。本轮已修。

本轮要回答两件事：
  1. 0x0703（按钮双击）在发过 0x0601 之后是否正常 —— 对照组必须先成立；
  2. 0x0503 能否提取出录音数据（0x0505/0x0506）—— 若能，
     「对戒指讲话」这条路就通了一大半，不必再猜怎么触发录音。

跑法（在自己的终端，按提示操作）：
    .venv312/bin/python -u tools/ring_audio_probe.py
"""
from __future__ import annotations

import asyncio
import struct
import sys
import time
from collections import Counter

from bleak import BleakClient, BleakScanner

sys.path.insert(0, ".")
from backend import zilo_protocol as zp  # noqa: E402

NOTIFY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
WRITE = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_SVC = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

frames: list[tuple[float, int, bytes]] = []
seen = Counter()
audio_bytes = bytearray()          # 0x0505/0x0506 攒下来的净荷
T0 = time.time()

KNOWN = {
    0x0102: "系统信息", 0x0401: "校时请求", 0x0605: "六轴", 0x0604: "停止上报ACK",
    0x0602: "开启上报ACK", 0x0701: "触摸手势", 0x0702: "动作手势",
    0x0703: "★按钮双击", 0x0502: "录音列表(回)", 0x0504: "提取应答",
    0x0505: "★录音数据", 0x0506: "★录音数据",
}


def on_notify(_, data: bytearray) -> None:
    try:
        f = zp.parse_frame(bytes(data))
    except Exception:
        print(f"  [{time.time()-T0:6.1f}s] 无法解析: {bytes(data).hex()}")
        return
    frames.append((time.time() - T0, f.cmd, f.body))
    seen[f.cmd] += 1
    if f.cmd in (0x0505, 0x0506):
        audio_bytes.extend(f.body)
    if f.cmd == zp.CMD_IMU_BATCH:
        return                                  # 六轴量大，只统计不刷屏
    body = f.body.hex()
    print(f"  [{time.time()-T0:6.1f}s] cmd=0x{f.cmd:04x} "
          f"{KNOWN.get(f.cmd, '未知'):<12} len={len(f.body):<5} "
          f"{body[:70]}{'…' if len(body) > 70 else ''}")


async def send(c: BleakClient, cmd: int, body: bytes = b"", label: str = "") -> None:
    fr = zp.build_frame(cmd, body)
    print(f"→ 0x{cmd:04x} {label}  {fr.hex()}")
    await c.write_gatt_char(WRITE, fr, response=False)


async def phase(title: str, seconds: int, instruction: str) -> int:
    before = len([f for f in frames if f[1] != zp.CMD_IMU_BATCH])
    print(f"\n{'='*66}\n【{title}】{instruction}\n{'='*66}")
    for s in range(seconds, 0, -1):
        print(f"\r  倒计时 {s:2d}s …", end="", flush=True)
        await asyncio.sleep(1)
    print("\r" + " " * 30 + "\r", end="")
    got = len([f for f in frames if f[1] != zp.CMD_IMU_BATCH]) - before
    print(f"  → 本阶段 {got} 帧（不含六轴）")
    return got


async def grab_ring(attempts: int = 8):
    """扫到就立刻连；广播窗口很短，慢一步就没了。"""
    for attempt in range(1, attempts + 1):
        hit: list = []
        ev = asyncio.Event()

        def cb(d, adv):
            name = d.name or adv.local_name or ""
            u = [x.lower() for x in (adv.service_uuids or [])]
            if (name.lower().startswith("ring") or NUS_SVC in u) and not hit:
                hit.append((d, adv.rssi))
                ev.set()

        print(f"\n[第 {attempt}/{attempts} 次] 扫描 —— 请**不停按戒指按钮**！")
        sc = BleakScanner(detection_callback=cb)
        await sc.start()
        try:
            await asyncio.wait_for(ev.wait(), timeout=25)
        except asyncio.TimeoutError:
            await sc.stop()
            print("  25s 内没广播，重来。")
            continue
        await sc.stop()
        dev, rssi = hit[0]
        print(f"  发现 {dev.address} rssi={rssi} —— 立刻连接（保持按键）…")
        client = BleakClient(dev, timeout=20.0)
        try:
            await client.connect()
            print("  ✓ 已连接")
            return client
        except Exception as e:
            print(f"  连接失败（{type(e).__name__}），重试…")
            try:
                await client.disconnect()
            except Exception:
                pass
    return None


def explain_0502(body: bytes) -> None:
    """把录音列表应答按几种可能的结构解一遍，肉眼挑合理的那个。"""
    print(f"\n  0x0502 body = {body.hex()}  ({len(body)} 字节)")
    if len(body) >= 6:
        err_u32 = struct.unpack(">I", body[:4])[0]
        rest_u16 = struct.unpack(">H", body[4:6])[0]
        print(f"    解法A  err(u32)={err_u32}  count(u16)={rest_u16}")
    if len(body) >= 5:
        err_u8 = body[0]
        print(f"    解法B  err(u8)={err_u8}  余 {body[1:].hex()}")
    print("    （err=0 表示成功；count 若为 63 说明戒指里已有 63 条录音）")


async def main() -> int:
    client = await grab_ring()
    if client is None:
        print("\n没连上。确认：不在充电座、按键唤醒、手机官方 App 已完全退出。")
        return 1

    try:
        await client.start_notify(NOTIFY, on_notify)
        await client.write_gatt_char(
            WRITE, zp.build_time_sync_ack(int(time.time())), response=False)
        await asyncio.sleep(0.4)

        await send(client, zp.CMD_SYS_INFO_REQ, label="系统信息")
        await asyncio.sleep(1.2)

        # ★ 上一轮漏掉的关键一步：不开上报就收不到按键/手势事件
        await send(client, zp.CMD_REPORT_START, label="开启上报（★上轮漏了这条）")
        await asyncio.sleep(1.5)

        # ---- 对照组：这一步不成立，后面所有结论都不算数 ----
        got = await phase("对照组：按钮双击", 10,
                          "请【快速双击】戒指按钮 2~3 次")
        has_703 = seen[0x0703] > 0
        print(f"  → 0x0703 收到 {seen[0x0703]} 次 "
              f"{'✓ 对照组成立' if has_703 else '✗ 仍未收到，下面结论存疑'}")

        # ---- 录音列表 ----
        print("\n" + "#" * 66)
        print("# 查录音列表")
        print("#" * 66)
        before_502 = seen[0x0502]
        await send(client, zp.CMD_AUDIO_LIST, label="录音列表")
        await asyncio.sleep(3)
        lst = [f for f in frames if f[1] == 0x0502]
        if lst:
            explain_0502(lst[-1][2])

        # ---- 核心实验：尝试提取 ----
        print("\n" + "#" * 66)
        print("# 尝试提取录音（0x0503）—— 用几种 payload 试")
        print("#" * 66)
        for label, payload in [
            ("空 payload", b""),
            ("index=0 (u32)", struct.pack(">I", 0)),
            ("index=1 (u32)", struct.pack(">I", 1)),
            ("index=0 (u16)", struct.pack(">H", 0)),
        ]:
            n0 = len(frames)
            await send(client, zp.CMD_AUDIO_EXTRACT_START, payload,
                       label=f"提取 {label}")
            await asyncio.sleep(4)
            print(f"    → 这次收到 {len(frames)-n0} 帧，"
                  f"累计音频字节 {len(audio_bytes)}")
            if audio_bytes:
                print("    ★ 有音频数据流出来了，停止试其它 payload")
                break

        if audio_bytes:
            await asyncio.sleep(6)      # 多等一会儿收完
            print(f"\n  共收到音频净荷 {len(audio_bytes)} 字节")

        # ---- 再看一次列表，比对是否变化 ----
        await send(client, zp.CMD_AUDIO_LIST, label="录音列表（再查）")
        await asyncio.sleep(3)

        await send(client, zp.CMD_REPORT_STOP, label="停止上报")
        await asyncio.sleep(0.5)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    # ---- 汇总 ----
    print("\n" + "=" * 66)
    print("命令统计")
    print("=" * 66)
    for cmd, n in seen.most_common():
        print(f"  0x{cmd:04x}  {n:6d} 次   {KNOWN.get(cmd, '未知')}")

    print(f"\n对照组 0x0703（按钮双击）：{seen[0x0703]} 次 "
          f"{'✓' if seen[0x0703] else '✗ —— 未成立，录音结论不可信'}")

    audio = [f for f in frames if 0x0500 <= f[1] <= 0x05FF]
    print(f"\n0x05xx 帧共 {len(audio)} 条：")
    for t, cmd, body in audio[:25]:
        print(f"  [{t:6.1f}s] 0x{cmd:04x} len={len(body)} {body.hex()[:80]}")

    if audio_bytes:
        out = "tools/ring_audio_dump.bin"
        with open(out, "wb") as fh:
            fh.write(audio_bytes)
        print(f"\n★ 音频数据已存 {out}（{len(audio_bytes)} 字节）—— 贴给我，我来判编码格式")
    else:
        print("\n没有任何音频数据流出。0x0503 的 payload 格式还没猜对，"
              "或该固件未开放提取。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
