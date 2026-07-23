"""连接戒指并导出完整 GATT 表 —— 这就是戒指的「API 表面」。

    .venv/bin/python tools/ble_dump.py <address>
    .venv/bin/python tools/ble_dump.py ring_sound        # 也可用名字（会先扫描匹配）

输出：所有 service / characteristic / descriptor 的 UUID + 属性(read/write/notify…)，
并自动把 read 的值 dump 出来（含 Device Information：厂商/型号/固件/序列号）。
最后给出「填进 zilo_adapter.js 的 SERVICE / NOTIFY / WRITE UUID」建议。

结果保存为 tools/gatt_dump.txt 方便贴回代码。
"""
import asyncio
import sys

from bleak import BleakClient, BleakScanner

# 标准 GATT 特征名（部分），让 dump 更可读
KNOWN = {
    "00002a29": "Manufacturer Name",
    "00002a24": "Model Number",
    "00002a25": "Serial Number",
    "00002a26": "Firmware Rev",
    "00002a27": "Hardware Rev",
    "00002a28": "Software Rev",
    "00002a19": "Battery Level",
    "00002a00": "Device Name",
}


def short(uuid: str) -> str:
    return uuid.split("-")[0].lower()


def _is_addr(target: str) -> bool:
    return "-" in target and len(target) >= 20 and target.count("-") >= 4


async def resolve(target: str):
    """target(名字或address) -> 新鲜的 BLEDevice 对象（macOS 连接前必须重新发现）。

    重试多轮扫描，因为戒指广播是断续的：请保持戒指唤醒（戴着 / 反复双击）。
    """
    by_addr = _is_addr(target)
    for attempt in range(1, 7):
        print(f"扫描第 {attempt}/6 轮，寻找 {'address' if by_addr else '名字'} {target!r} …（保持戒指唤醒）")
        if by_addr:
            dev = await BleakScanner.find_device_by_address(target, timeout=8)
        else:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, a: target.lower() in ((d.name or a.local_name or "").lower()), timeout=8
            )
        if dev:
            print(f"✓ 捕获 {dev.name or '(无名)'} @ {dev.address}")
            return dev
    print("多轮扫描都没抓到戒指。请确认戒指已取出充电盒、且在反复双击保持唤醒，再重试。")
    sys.exit(1)


async def main(target: str) -> None:
    dev = await resolve(target)
    lines: list[str] = []

    def out(s=""):
        print(s); lines.append(s)

    out(f"连接 {dev.address} …")
    async with BleakClient(dev, timeout=20) as client:
        out(f"已连接: {client.is_connected}\n")
        notify_chars, write_chars, main_service = [], [], None

        for service in client.services:
            out(f"■ SERVICE {service.uuid}  ({service.description})")
            for ch in service.characteristics:
                props = ",".join(ch.properties)
                label = KNOWN.get(short(ch.uuid), "")
                out(f"  ├─ CHAR {ch.uuid}  [{props}]  {label}")
                if "notify" in ch.properties or "indicate" in ch.properties:
                    notify_chars.append((service.uuid, ch.uuid))
                if "write" in ch.properties or "write-without-response" in ch.properties:
                    write_chars.append((service.uuid, ch.uuid))
                    if not short(service.uuid).startswith("0000180"):
                        main_service = service.uuid
                if "read" in ch.properties:
                    try:
                        val = await client.read_gatt_char(ch.uuid)
                        txt = val.decode("utf-8", "replace").strip("\x00") if val else ""
                        printable = txt if txt.isprintable() and txt else val.hex()
                        out(f"  │     read = {printable!r}")
                    except Exception as e:
                        out(f"  │     read 失败: {e}")
                for d in ch.descriptors:
                    out(f"  │   · descriptor {d.uuid}")
            out("")

        out("=" * 70)
        out("填进 client/logic/zilo_adapter.js 的建议：")
        out(f"  ZILO_SERVICE_UUID   = '{main_service or (notify_chars[0][0] if notify_chars else '?')}'")
        if notify_chars:
            out(f"  ZILO_NOTIFY_CHAR_UUID = '{notify_chars[0][1]}'   (上行 notify)")
        if write_chars:
            out(f"  ZILO_WRITE_CHAR_UUID  = '{write_chars[0][1]}'   (下行 write)")
        out("")
        out("下一步（发命令 / 抓事件）：")
        n = notify_chars[0][1] if notify_chars else "<notify_uuid>"
        w = write_chars[0][1] if write_chars else "<write_uuid>"
        out(f"  .venv/bin/python tools/ble_probe.py {dev.address} {n} {w}")

    with open("tools/gatt_dump.txt", "w") as f:
        f.write("\n".join(lines))
    print("\n(已保存 tools/gatt_dump.txt)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: ble_dump.py <address|name>"); sys.exit(1)
    asyncio.run(main(sys.argv[1]))
