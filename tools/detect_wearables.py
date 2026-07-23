"""同时检测「戒指 + 小米/常见手表」的蓝牙广播 —— 确认两个设备都在场、可被发现。

用法：
    .venv/bin/python -u tools/detect_wearables.py 20      # 扫描 20 秒

会把扫到的设备分三类打印：
  👉 RING   : 名字含 ring（Zilo 戒指）—— 可直连读数据
  ⌚ WATCH  : 小米/Amazfit/华为/Zepp 等手表 —— 能扫到广播，但数据要走 Gadgetbridge
  ·  OTHER : 其它

重要：手表「扫到」≠「能读数据」。小米手表 BLE 加密+绑定，健康数据必须用
Gadgetbridge(安卓)配对后导出/转发，不能从电脑裸读（见 backend/gadgetbridge.py）。
"""
import asyncio
import sys

from bleak import BleakScanner

RING_HINTS = ("ring", "zilo")
# 常见手表/手环品牌关键字（名字）与厂商 ID（BLE company identifier）
WATCH_NAME_HINTS = ("mi band", "xiaomi", "amazfit", "zepp", "redmi", "huawei band",
                    "huawei watch", "watch", "band", "gts", "gtr", "bip")
WATCH_COMPANY_IDS = {0x0157, 0x038F, 0x027D}  # Huami/Xiaomi/Huawei 常见（近似，仅提示）

_seen: dict = {}


def classify(name: str, mfg: dict) -> str:
    low = name.lower()
    if any(h in low for h in RING_HINTS):
        return "ring"
    if any(h in low for h in WATCH_NAME_HINTS):
        return "watch"
    if any(cid in WATCH_COMPANY_IDS for cid in mfg):
        return "watch?"
    return "other"


async def main(duration: float):
    print(f"扫描 {duration:.0f}s —— 让戒指(取出/双击)和手表都保持开机在附近 …\n")

    def cb(dev, adv):
        name = (dev.name or adv.local_name or "").strip()
        mfg = adv.manufacturer_data or {}
        cls = classify(name, mfg)
        key = dev.address
        prev = _seen.get(key)
        # ring 每次刷新，其它只记录一次（保留最强信号）
        if prev and cls not in ("ring",):
            return
        _seen[key] = (cls, name, adv.rssi, list(mfg.keys()))

    async with BleakScanner(cb):
        await asyncio.sleep(duration)

    rings = [(k, v) for k, v in _seen.items() if v[0] == "ring"]
    watches = [(k, v) for k, v in _seen.items() if v[0] in ("watch", "watch?")]

    print("=" * 66)
    print("👉 戒指 (可直连读数据):")
    if rings:
        for k, (_, n, rssi, _) in rings:
            print(f"   RSSI={rssi:>4}  name={n!r}  {k}")
        print("   → 下一步真机读数据: .venv/bin/python -u tools/ring_session.py 60")
    else:
        print("   ✗ 没扫到。戒指从充电盒取出、双击唤醒后重试。")

    print("\n⌚ 手表 (只能扫到广播；数据需 Gadgetbridge 安卓):")
    if watches:
        for k, (c, n, rssi, cids) in watches:
            tag = "确认" if c == "watch" else "疑似"
            print(f"   [{tag}] RSSI={rssi:>4}  name={n or '(无名)'!r}  company={[hex(c) for c in cids]}  {k}")
        print("   → 数据路径: 安卓装 Gadgetbridge → 配对手表 → 导出 SQLite")
        print("               或 转发到后端 /ws/watch（见 backend/main.py）")
    else:
        print("   ✗ 没扫到手表广播。确认手表开机、蓝牙开、在附近。")
        print("   (注意：即使扫到，也不能裸读数据——必须 Gadgetbridge)")

    print("\n共扫到设备:", len(_seen))


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    asyncio.run(main(dur))
