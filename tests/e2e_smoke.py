"""端到端冒烟：起服务，两个 WS 用户走完 发现→提醒→双确认→交换→Agent。
运行: python tests/e2e_smoke.py"""
import asyncio
import json
import sys

import websockets

BASE = "ws://127.0.0.1:8000"


async def drain_until(ws, wanted_types: set[str], timeout=8):
    got = {}
    try:
        while wanted_types - set(got):
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            got.setdefault(msg["type"], msg)
    except asyncio.TimeoutError:
        pass
    return got


async def main():
    a = await websockets.connect(f"{BASE}/ws/user/u_demo_a")
    b = await websockets.connect(f"{BASE}/ws/user/u_demo_b")
    await drain_until(a, {"state"}); await drain_until(b, {"state"})

    # 1. 双方切绿
    await a.send(json.dumps({"action": "set_mode", "mode": "friend"}))
    await b.send(json.dumps({"action": "set_mode", "mode": "friend"}))
    await drain_until(a, {"state"}); await drain_until(b, {"state"})

    # 2. A 连续上报 3 次扫描（模拟 BLE 持续采样）
    for _ in range(3):
        await a.send(json.dumps({"action": "sighting",
                                 "ephemeral_id": "eph_u_demo_b", "rssi": -60}))
        await asyncio.sleep(0.05)

    got_a = await drain_until(a, {"match_notice"})
    got_b = await drain_until(b, {"match_notice"})
    assert "match_notice" in got_a and "match_notice" in got_b, "双方应收到匹配提醒"
    print("✓ 双方收到匹配提醒 score=", got_a["match_notice"]["match_score"])

    # 3. 双方各自双击确认（mock 按钮）
    await a.send(json.dumps({"action": "mock_button"}))
    got_a2 = await drain_until(a, {"self_confirmed"})
    assert "self_confirmed" in got_a2, "A 应收到单方确认回执"
    print("✓ A 已确认，等待对方")

    await b.send(json.dumps({"action": "mock_button"}))
    got_a3 = await drain_until(a, {"encounter", "agent_content"})
    got_b3 = await drain_until(b, {"encounter", "agent_content"})
    assert "encounter" in got_a3 and "encounter" in got_b3, "双确认应建立 Encounter"
    card_a = got_a3["encounter"]["card"]
    assert "wechat" in card_a and "shared_interests" in card_a
    assert "phone" not in card_a
    print("✓ Encounter 建立，A 看到的卡:", card_a)
    assert "agent_content" in got_a3, "Agent 内容（或 fallback）应到达"
    print("✓ Agent:", got_a3["agent_content"]["icebreaker"])

    # 4. 切蓝立即失效验证：读到 BLUE_OFFLINE 为止
    await a.send(json.dumps({"action": "set_mode", "mode": "off"}))
    final_state = None
    try:
        while final_state != "BLUE_OFFLINE":
            msg = json.loads(await asyncio.wait_for(a.recv(), timeout=5))
            if msg["type"] == "state":
                final_state = msg["state"]
    except asyncio.TimeoutError:
        pass
    assert final_state == "BLUE_OFFLINE", f"期望 BLUE_OFFLINE，得到 {final_state}"
    print("✓ 切蓝立即生效")

    await a.close(); await b.close()
    print("\n端到端主路径 PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e); sys.exit(1)
