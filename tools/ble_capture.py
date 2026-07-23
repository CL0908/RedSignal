"""常驻自动抓包 —— 循环 [扫描→连上戒指→订阅→记录所有上行帧→断线重来]。

解决「戒指一会儿就睡、每次都要卡时机」的问题：本脚本一直挂着，
你随时唤醒戒指、随便操作（双击/长按/旋转/装脱），帧都会被记录。

    .venv/bin/python -u tools/ble_capture.py 300        # 挂 300 秒
    .venv/bin/python -u tools/ble_capture.py 300 --poke # 连上后主动发一串命令探测

所有上行帧写入 tools/capture_log.txt（含时间戳、cmd 解析、原始 hex）。
"""
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify 上行
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write 下行

CMD_NAMES = {
    0x0101: "SYS_INFO_REQ", 0x0102: "SYS_INFO_RESP",
    0x0601: "START_REPORT", 0x0603: "STOP_REPORT", 0x0605: "IMU_BATCH",
    0x0701: "TOUCH_DOUBLE_TAP", 0x0702: "MOTION_GESTURE", 0x0703: "BUTTON_DOUBLE_PRESS",
}

LOG_PATH = "tools/capture_log.txt"
_count = 0

# 周期心跳帧特征：cmd=3f00 且 body 以此开头。凡不匹配者 = 新种帧（可能是按钮/手势事件）
HEARTBEAT_SIG = bytes.fromhex("04040100000004")


def is_heartbeat(data: bytes) -> bool:
    return len(data) >= 2 and data[:2] == b"\x3f\x00" and data[2:2 + len(HEARTBEAT_SIG)] == HEARTBEAT_SIG


def describe(data: bytes) -> str:
    if len(data) < 2:
        return f"raw({len(data)}) {data.hex()}"
    cmd = struct.unpack(">H", data[:2])[0]
    name = CMD_NAMES.get(cmd, "UNKNOWN")
    body = data[2:]
    txt = ""
    printable = bytes(b for b in body if 32 <= b < 127)
    if len(printable) >= max(3, len(body) // 2):
        txt = f"  ascii={body.decode('utf-8', 'replace')!r}"
    return f"cmd=0x{cmd:04X}({name}) blen={len(body)} body={body.hex()}{txt}"


def logline(s: str):
    print(s, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(s + "\n")


async def poke(client: BleakClient):
    """连上后主动发一串候选命令，看戒指是否回话。"""
    cands = [0x0101, 0x0601, 0x0301, 0x0201, 0x0401, 0x0501, 0x0801, 0x0901]
    for cmd in cands:
        frame = struct.pack(">H", cmd)
        for resp in (False, True):
            try:
                await client.write_gatt_char(NUS_RX, frame, response=resp)
                logline(f"  ⬇ poke 0x{cmd:04X} (response={resp}) sent")
                break
            except Exception as e:
                logline(f"  ⬇ poke 0x{cmd:04X} (response={resp}) 失败 {e}")
        await asyncio.sleep(0.6)


async def session(dev, do_poke: bool):
    def on_notify(_h, data: bytes):
        global _count
        _count += 1
        data = bytes(data)
        ts = time.strftime("%H:%M:%S")
        if is_heartbeat(data):
            logline(f"[{ts}] ⬆ #{_count} [心跳] {describe(data)}")
        else:
            logline(f"[{ts}] ★★★ 新种帧 #{_count} ★★★ {describe(data)}  raw={data.hex()}")

    async with BleakClient(dev, timeout=20) as client:
        logline(f"★ 已连接 {dev.address}  (随便操作戒指，帧会被记录)")
        await client.start_notify(NUS_TX, on_notify)
        if do_poke:
            await poke(client)
        # 保持连接直到断线；每 0.5s 检查一次
        while client.is_connected:
            await asyncio.sleep(0.5)
    logline("… 连接断开，回到扫描重连")


async def main(duration: float, do_poke: bool):
    open(LOG_PATH, "w").close()
    logline(f"=== 常驻抓包开始，共 {duration:.0f}s。请随时唤醒并操作戒指 ===")
    deadline = None  # 用循环计数近似，避免 time 依赖
    elapsed = 0.0
    while elapsed < duration:
        dev = await BleakScanner.find_device_by_filter(
            lambda d, a: "ring" in ((d.name or a.local_name or "").lower()), timeout=6
        )
        elapsed += 6
        if not dev:
            logline(f"[{time.strftime('%H:%M:%S')}] 扫描中…未见戒指广播（唤醒它=从盒取出/双击/晃动）")
            continue
        logline(f"[{time.strftime('%H:%M:%S')}] 发现 {dev.name} @ {dev.address}，连接…")
        try:
            await session(dev, do_poke)
        except Exception as e:
            logline(f"连接/会话异常：{e}")
        elapsed += 2
    logline(f"=== 抓包结束，共记录 {_count} 帧。日志：{LOG_PATH} ===")


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else 300.0
    poke_flag = "--poke" in sys.argv
    asyncio.run(main(dur, poke_flag))
