"""连续 BLE 监视 —— 过滤 Apple 噪声，实时打印新出现的可疑设备。

一边运行本脚本，一边把戒指从充电盒取出 / 戴上 / 双击唤醒，
戒指一开始广播就会在这里冒出来（尤其名字含 ring_sound 或带非 Apple 厂商数据）。

    .venv/bin/python tools/ble_watch.py            # 监视 60 秒
    .venv/bin/python tools/ble_watch.py 120

Ctrl-C 结束。命中 ring/zilo/sound 会高亮并提示下一步。
"""
import asyncio
import sys

from bleak import BleakScanner

APPLE = 76  # 0x004C，AirPods/iPhone/Mac 噪声，过滤掉
RING_HINTS = ("ring", "zilo", "sound")
_printed: set[str] = set()


def interesting(name: str, mfg: dict) -> bool:
    if any(h in name.lower() for h in RING_HINTS):
        return True
    if name and name not in ("", "(无名)"):
        return True  # 任何有名字的非苹果设备都值得看
    # 无名但带非苹果厂商数据
    return any(cid != APPLE for cid in mfg)


async def main(duration: float) -> None:
    print(f"监视 {duration:.0f}s —— 现在把戒指取出充电盒 / 戴上 / 双击唤醒 …\n")

    def cb(device, adv):
        name = (device.name or adv.local_name or "").strip()
        mfg = adv.manufacturer_data or {}
        key = device.address
        is_ring = any(h in name.lower() for h in RING_HINTS)
        # 命中戒指关键字每次都打；其它设备只打一次
        if not is_ring and key in _printed:
            return
        if not interesting(name, mfg):
            return
        _printed.add(key)
        uuids = list(adv.service_uuids or [])
        mfg_s = ", ".join(f"{k:#x}:{v.hex()}" for k, v in mfg.items())
        tag = "👉 疑似戒指 " if is_ring else "   "
        print(f"{tag}RSSI={adv.rssi:>4}  name={name or '(无名)':<24} {device.address}")
        if uuids:
            print(f"        service_uuids={uuids}")
        if mfg_s:
            print(f"        mfg=[{mfg_s}]")
        if is_ring:
            print(f"        ↳ 下一步: .venv/bin/python tools/ble_dump.py {device.address}\n")

    async with BleakScanner(cb):
        await asyncio.sleep(duration)

    print("\n监视结束。若没看到戒指：确认已取出充电盒、戒指有电、蓝牙权限已给。")


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    asyncio.run(main(dur))
