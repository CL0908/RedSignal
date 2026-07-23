"""戒指 0x0103「修改系统配置」盲探针 —— 尝试找出隐藏的灯/振动控制。

⚠️ 实验性 + 有风险：0x0103 是把任意字节写进固件配置。schema 未公开（APK 里就是个
   手输 hex 的调试框）。写入未知配置**可能改变真实设置**，戒指无电源开关不易复位。
   成功找到灯控制的概率不高，灯是否可 BLE 控制本身也无保证。请知情后再用。

用法：
  # 单发一个 payload，读 0x0104 结果，同时用眼睛看灯有没有变（最推荐、最可控）
  .venv/bin/python -u tools/ring_config_probe.py --payload 0101

  # 扫描：对一小段 config-id 依次试 [id 01]/[id 00]/[id 01 01] 等短包，每包停顿看灯
  .venv/bin/python -u tools/ring_config_probe.py --sweep

0x0104 errorCode 含义：0成功 1未知 2忙 3文件不存在 4命令组不存在 5命令不存在
                       6超时 7参数异常 8通讯异常 9Flash不足
经验：errorCode=0 = 固件**受理了**这个配置（值得盯灯）；7 = 形式/值不对，换一个。
"""
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

sys.path.insert(0, ".")
from backend import zilo_protocol as zp  # noqa: E402

NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
ERR = {0: "成功", 1: "未知", 2: "忙", 3: "文件不存在", 4: "命令组不存在",
       5: "命令不存在", 6: "超时", 7: "参数异常", 8: "通讯异常", 9: "Flash不足"}


async def resolve(name="ring"):
    for i in range(1, 8):
        print(f"扫描 {i}/7 找 {name!r} …（保持戒指唤醒）")
        dev = await BleakScanner.find_device_by_filter(
            lambda d, a: name in ((d.name or a.local_name or "").lower()), timeout=8)
        if dev:
            print(f"✓ {dev.name} @ {dev.address}"); return dev
    print("没抓到戒指。"); sys.exit(1)


def parse_0104(body: bytes) -> str:
    if len(body) < 2:
        return f"payload={body.hex()}"
    err = struct.unpack(">H", body[:2])[0]
    return f"errorCode={err}({ERR.get(err,'?')}) payload={body[2:].hex() or '(空)'}"


def _candidates() -> list[bytes]:
    """短小的配置猜测：多种「id + 值」形状。故意小、可逆倾向、逐个可观察。"""
    cands: list[bytes] = []
    for cid in range(0x00, 0x14):          # config-id 0..0x13
        cands.append(bytes([cid, 0x01]))          # [id][01]  开?
        cands.append(bytes([cid, 0x00]))          # [id][00]  关?
        cands.append(bytes([cid, 0x01, 0x01]))    # [id][len][val] TLV?
    # 一些「颜色/闪烁」直觉猜测（RGB / 频率），前缀几个可能的 LED id
    for cid in (0x0A, 0x0B, 0x0C, 0x10, 0x11):
        cands.append(bytes([cid, 0xFF, 0x00, 0x00]))   # 红?
        cands.append(bytes([cid, 0x00, 0xFF, 0x00]))   # 绿?
        cands.append(bytes([cid, 0x03, 0xFF, 0x00, 0x00]))  # [id][len][rgb]?
    return cands


async def main():
    args = sys.argv[1:]
    one = None
    if "--payload" in args:
        one = bytes.fromhex(args[args.index("--payload") + 1].replace(" ", ""))
    sweep = "--sweep" in args

    print("=" * 66)
    print("⚠️  实验：向 0x0103 写未知配置。可能改变真实设置，成功率低。")
    print("=" * 66)
    dev = await resolve()
    got = {"resp": None}

    async with BleakClient(dev, timeout=20) as client:
        print("已连接。\n")

        def on_notify(_h, data: bytes):
            data = bytes(data)
            try:
                f = zp.parse_frame(data)
            except Exception:
                return
            if f.cmd == zp.CMD_TIME_SYNC_REQ:
                asyncio.get_event_loop().create_task(
                    client.write_gatt_char(NUS_RX, zp.build_time_sync_ack(int(time.time())), response=False))
            elif f.cmd == 0x0104:   # 系统配置修改结果
                got["resp"] = parse_0104(f.body)
                print(f"   ⬆ 0x0104 {got['resp']}")

        await client.start_notify(NUS_TX, on_notify)
        # 主动回一次校时，避免戒指刷 0x0401 干扰
        await client.write_gatt_char(NUS_RX, zp.build_time_sync_ack(int(time.time())), response=False)
        await asyncio.sleep(0.5)

        async def send_0103(body: bytes):
            got["resp"] = None
            frame = zp.build_frame(0x0103, body)
            print(f"⬇ 0x0103 body={body.hex()}  → 看戒指的灯有没有变化！")
            await client.write_gatt_char(NUS_RX, frame, response=False)
            await asyncio.sleep(1.6)   # 留时间收 0104 + 观察灯

        if one is not None:
            await send_0103(one)
        elif sweep:
            print("=== 扫描 config 猜测（每包 1.6s，请全程盯着戒指的灯）===\n")
            for i, c in enumerate(_candidates(), 1):
                print(f"[{i}/{len(_candidates())}]", end=" ")
                await send_0103(c)
            print("\n扫描完毕。若某一包让灯变了/闪了，记下它上面打印的 body= 即命中。")
        else:
            print("用法：--payload <hex>  或  --sweep")
        await asyncio.sleep(0.5)
        try:
            await client.stop_notify(NUS_TX)
        except Exception:
            pass
    print("\n完成。命中特征：0x0104 errorCode=0 且灯有肉眼变化。")


if __name__ == "__main__":
    asyncio.run(main())
