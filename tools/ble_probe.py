"""向戒指发命令、抓上行帧、并可选地「扫描命令空间」发掘 API。

    # 基础：订阅 notify，发已知命令（系统信息 + 开启上报），实时打印回帧
    .venv/bin/python tools/ble_probe.py <addr> <notify_uuid> <write_uuid>

    # 追加：扫描命令空间——遍历一批 cmd 码，看哪些有响应（发掘未文档化 API）
    .venv/bin/python tools/ble_probe.py <addr> <notify_uuid> <write_uuid> --sweep

    # 只发某个命令：
    .venv/bin/python tools/ble_probe.py <addr> <notify_uuid> <write_uuid> --cmd 0x0101

帧格式（见 backend/zilo_protocol.py）：[cmd 2字节大端][body...]，奇=请求，偶=响应。
所有收到的帧会解析并保存到 tools/probe_log.txt。
"""
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

# 已知命令（来自 zilo_protocol.py）
CMD_NAMES = {
    0x0101: "SYS_INFO_REQ", 0x0102: "SYS_INFO_RESP",
    0x0601: "START_REPORT", 0x0603: "STOP_REPORT", 0x0605: "IMU_BATCH",
    0x0701: "TOUCH_DOUBLE_TAP", 0x0702: "MOTION_GESTURE", 0x0703: "BUTTON_DOUBLE_PRESS",
}
_log: list[str] = []


def build(cmd: int, body: bytes = b"") -> bytes:
    return struct.pack(">H", cmd) + body


def describe(data: bytes) -> str:
    if len(data) < 2:
        return f"raw {data.hex()}"
    cmd = struct.unpack(">H", data[:2])[0]
    name = CMD_NAMES.get(cmd, "UNKNOWN")
    body = data[2:]
    extra = ""
    if cmd == 0x0102 and body:  # 系统信息响应，尝试文本化
        extra = f"  → text={body.decode('utf-8','replace')!r}"
    return f"cmd=0x{cmd:04X}({name}) len(body)={len(body)} body={body.hex()}{extra}"


def _is_addr(t: str) -> bool:
    return "-" in t and len(t) >= 20 and t.count("-") >= 4


async def resolve(target: str):
    """名字/address -> 新鲜 BLEDevice（macOS 连接前须重新发现）。多轮重试。"""
    by_addr = _is_addr(target)
    for attempt in range(1, 7):
        print(f"扫描第 {attempt}/6 轮，寻找 {target!r} …（保持戒指唤醒）")
        if by_addr:
            dev = await BleakScanner.find_device_by_address(target, timeout=8)
        else:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, a: target.lower() in ((d.name or a.local_name or "").lower()), timeout=8
            )
        if dev:
            print(f"✓ 捕获 {dev.name or '(无名)'} @ {dev.address}")
            return dev
    print("多轮扫描没抓到戒指，请保持唤醒后重试。"); sys.exit(1)


async def main() -> None:
    target, notify_uuid, write_uuid = sys.argv[1], sys.argv[2], sys.argv[3]
    rest = sys.argv[4:]
    sweep = "--sweep" in rest
    listen = "--listen" in rest
    listen_s = 30.0
    if listen and rest.index("--listen") + 1 < len(rest):
        try:
            listen_s = float(rest[rest.index("--listen") + 1])
        except ValueError:
            pass
    one_cmd = None
    if "--cmd" in rest:
        one_cmd = int(rest[rest.index("--cmd") + 1], 16)

    def on_notify(_h, data: bytes):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] ⬆ {describe(bytes(data))}"
        print(line); _log.append(line)

    dev = await resolve(target)
    print(f"连接 {dev.address} …")
    async with BleakClient(dev, timeout=20) as client:
        print(f"已连接: {client.is_connected}")
        await client.start_notify(notify_uuid, on_notify)
        print(f"已订阅 notify {notify_uuid}\n")

        async def send(cmd: int, body: bytes = b""):
            frame = build(cmd, body)
            name = CMD_NAMES.get(cmd, "UNKNOWN")
            print(f"⬇ 发送 cmd=0x{cmd:04X}({name}) frame={frame.hex()}")
            _log.append(f"⬇ send 0x{cmd:04X} {frame.hex()}")
            await client.write_gatt_char(write_uuid, frame, response=False)

        if listen:
            # 纯受动：不发任何命令，观察戒指自发上行帧（物理操作时的真实事件格式）
            print(f"=== 受动监听 {listen_s:.0f}s：现在双击/长按/旋转/装脱戒指，观察它自发上报什么 ===\n")
            await asyncio.sleep(listen_s)
        elif one_cmd is not None:
            await send(one_cmd)
            await asyncio.sleep(3)
        elif sweep:
            # 扫描命令空间：对每个「组」发奇数(请求)码，观察是否有响应
            print("=== 命令空间扫描：发请求码，看谁回话（发掘未文档化 API）===\n")
            groups = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B]
            for g in groups:
                for sub in (0x01, 0x03, 0x05, 0x07):
                    cmd = (g << 8) | sub
                    await send(cmd)
                    await asyncio.sleep(0.4)
            print("\n扫描完毕，再等 3s 收尾包 …")
            await asyncio.sleep(3)
        else:
            # 默认流程：系统信息 → 开启上报 → 收 10s 事件 → 停止
            await send(0x0101)                # 系统信息
            await asyncio.sleep(1.5)
            await send(0x0601)                # 开启事件/IMU 上报
            print("\n上报已开启：现在双击戒指按钮 / 转动戒指，观察上行帧 10s …\n")
            await asyncio.sleep(10)
            await send(0x0603)                # 停止
            await asyncio.sleep(1)

        try:
            await client.stop_notify(notify_uuid)
        except Exception:
            pass

    with open("tools/probe_log.txt", "w") as f:
        f.write("\n".join(_log))
    print("\n(已保存 tools/probe_log.txt)")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("用法: ble_probe.py <addr> <notify_uuid> <write_uuid> [--sweep|--cmd 0xNNNN]")
        sys.exit(1)
    asyncio.run(main())
