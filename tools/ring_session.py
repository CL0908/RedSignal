"""真机会话 —— 用**逆向出的真实协议**与戒指正确对话并提取资料。

复用 backend/zilo_protocol.py（单一真实源），流程：
  连接 → 订阅 notify
  → 主动回应戒指的 0x0401 校时请求（否则它一直重发）
  → 发 0x0101 获取系统信息（打印固件/电量/SN/型号）
  → 发 0x0601 开启实时六轴上报
  → 监听 N 秒：按钮双击(0x0703)/手势(0x0702)/六轴(0x0605) 实时打印

    .venv/bin/python -u tools/ring_session.py 40    # 会话 40 秒

在“会话中”按戒指按钮双击，会看到 ★ 按钮双击确认。
"""
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

sys.path.insert(0, ".")
from backend import zilo_protocol as zp  # noqa: E402

NUS_TX = zp_notify = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"


async def resolve(name="ring"):
    for i in range(1, 8):
        print(f"扫描 {i}/7 找 {name!r} …（保持戒指唤醒）")
        dev = await BleakScanner.find_device_by_filter(
            lambda d, a: name in ((d.name or a.local_name or "").lower()), timeout=8
        )
        if dev:
            print(f"✓ {dev.name} @ {dev.address}")
            return dev
    print("没抓到戒指，保持唤醒后重试。"); sys.exit(1)


async def main(duration: float):
    dev = await resolve()
    got = {"button": 0, "gesture": 0, "imu": 0, "sysinfo": None}

    async with BleakClient(dev, timeout=20) as client:
        print("已连接。开始会话。\n")

        async def write(frame: bytes, tag: str):
            try:
                await client.write_gatt_char(NUS_RX, frame, response=False)
                print(f"  ⬇ {tag}: {frame.hex()}")
            except Exception as e:
                print(f"  ⬇ {tag} 写失败: {e}")

        def on_notify(_h, data: bytes):
            data = bytes(data)
            try:
                frame = zp.parse_frame(data)
            except Exception as e:
                print(f"⬆ 坏帧 {data.hex()} ({e})"); return
            cmd = frame.cmd
            crc = "" if frame.crc_valid else " ⚠CRC坏"
            if cmd == zp.CMD_TIME_SYNC_REQ:  # 0x0401 戒指求时间 → 回应
                ack = zp.build_time_sync_ack(int(time.time()))
                asyncio.get_event_loop().create_task(_reply(client, ack))
                print(f"⬆ 0x0401 校时请求{crc} → 已回 0x0402")
            elif cmd == zp.CMD_SYS_INFO_RESP:  # 0x0102
                info = zp.parse_sys_info(frame.body)
                got["sysinfo"] = info
                print(f"⬆ ★ 系统信息{crc}: 固件={info.get('firmwareVersion')!r} "
                      f"电量={info.get('batteryPercent')}% 充电={info.get('batteryCharging')} "
                      f"SN={info.get('sn')!r} 型号={info.get('model')!r}")
            elif cmd == zp.CMD_BUTTON_DOUBLE_PRESS:  # 0x0703
                got["button"] += 1
                print(f"⬆ ★★★ 按钮双击确认 #{got['button']}{crc}  payload={frame.body.hex()}")
            elif cmd in (zp.CMD_SENSOR_GESTURE, zp.CMD_GESTURE_RECOGNITION):
                gid = frame.body[4] if len(frame.body) >= 5 else 0
                got["gesture"] += 1
                print(f"⬆ ★ 手势 {zp.GESTURE_NAMES.get(gid, gid)}{crc}")
            elif cmd == zp.CMD_IMU_BATCH:  # 0x0605
                batch = zp._parse_imu_batch("u", frame.body)
                n = len(batch.accel) if batch else 0
                got["imu"] += 1
                if got["imu"] % 10 == 1:
                    a0 = batch.accel[0] if batch and batch.accel else None
                    print(f"⬆ 六轴帧 x{got['imu']}（本帧 {n} 采样，首帧 accel={a0}）")
            else:
                print(f"⬆ {frame.label} (0x{cmd:04X}){crc} body={frame.body.hex()}")

        await client.start_notify(NUS_TX, on_notify)
        # 主动握手
        await write(zp.build_time_sync_ack(int(time.time())), "0x0402 校时应答(主动)")
        await asyncio.sleep(0.3)
        await write(zp.build_frame(zp.CMD_SYS_INFO_REQ), "0x0101 获取系统信息")
        await asyncio.sleep(0.5)
        await write(zp.build_frame(zp.CMD_START_REPORT), "0x0601 开启六轴上报")
        print(f"\n=== 会话中 {duration:.0f}s：请按戒指按钮双击 / 做手势 / 转戒指 ===\n")
        await asyncio.sleep(duration)
        await write(zp.build_frame(zp.CMD_STOP_REPORT), "0x0603 停止上报")
        await asyncio.sleep(0.5)
        try:
            await client.stop_notify(NUS_TX)
        except Exception:
            pass

    print("\n=== 会话小结 ===")
    print(f"  系统信息: {got['sysinfo']}")
    print(f"  按钮双击: {got['button']} 次   手势: {got['gesture']} 次   六轴帧: {got['imu']} 帧")


async def _reply(client, frame):
    try:
        await client.write_gatt_char(NUS_RX, frame, response=False)
    except Exception:
        pass


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    asyncio.run(main(dur))
