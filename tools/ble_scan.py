"""BLE 扫描 —— 找到 Zilo 戒指 (ring_sound) 并列出周边设备。

用法：
    .venv/bin/python tools/ble_scan.py            # 扫描 12 秒
    .venv/bin/python tools/ble_scan.py 20         # 扫描 20 秒

macOS 首次运行会弹「终端要访问蓝牙」权限，需允许。
CoreBluetooth 不暴露 MAC，设备用系统 UUID 标识（连接时用这个 UUID）。
"""
import asyncio
import sys

from bleak import BleakScanner

RING_HINTS = ("ring", "zilo", "sound")


async def main(duration: float) -> None:
    print(f"扫描 {duration:.0f}s …（把戒指从充电盒取出、或保持充电都试试）\n")
    seen: dict[str, tuple] = {}

    def cb(device, adv):
        name = (device.name or adv.local_name or "").strip()
        uuids = list(adv.service_uuids or [])
        seen[device.address] = (name, adv.rssi, uuids, adv.manufacturer_data)

    async with BleakScanner(cb):
        await asyncio.sleep(duration)

    if not seen:
        print("没有发现任何 BLE 设备。检查蓝牙是否开启 / 终端蓝牙权限。")
        return

    rows = sorted(seen.items(), key=lambda kv: kv[1][1], reverse=True)
    print(f"{'RSSI':>5}  {'NAME':<22} ADDRESS(系统UUID)")
    print("-" * 78)
    ring_hits = []
    for addr, (name, rssi, uuids, mfg) in rows:
        disp = name or "(无名)"
        line = f"{rssi:>5}  {disp:<22} {addr}"
        is_ring = any(h in name.lower() for h in RING_HINTS)
        if is_ring:
            line = "👉 " + line
            ring_hits.append((addr, name, uuids, mfg))
        print(line)
        if uuids:
            print(f"       service_uuids: {uuids}")
        if mfg:
            print(f"       mfg_data: {{ {', '.join(f'{k}:{v.hex()}' for k,v in mfg.items())} }}")

    print("\n" + "=" * 78)
    if ring_hits:
        print("疑似戒指设备：")
        for addr, name, uuids, _ in ring_hits:
            print(f"  name={name!r}  address={addr}")
        print("\n下一步： .venv/bin/python tools/ble_dump.py <address>")
    else:
        print("未匹配到 ring/zilo/sound 关键字。若戒指在上面列表里，按名字/信号强度判断，")
        print("然后： .venv/bin/python tools/ble_dump.py <address>")


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    asyncio.run(main(dur))
