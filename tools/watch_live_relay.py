"""通过 adb logcat 实时抓取 Gadgetbridge 的健康数据并转发给后端。

原理：Gadgetbridge 在同步手表数据时会写日志。我们监听 logcat 中 GB 相关的
心率/步数/电量日志行，解析后 POST 到后端 /api/devices/{user_id}/watch。

这是 hack 方案——不需要 root、不需要导出 DB。只要 GB 在前台或后台运行
且与手表保持连接，同步时数据就会出现在 logcat 中。

用法:
  python3 tools/watch_live_relay.py [user_id] [backend_url]

默认:
  user_id = u_demo_a
  backend_url = http://localhost:8000
"""
import json
import re
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

USER_ID = sys.argv[1] if len(sys.argv) > 1 else "u_demo_a"
BACKEND = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"
API_URL = f"{BACKEND}/api/devices/{USER_ID}/watch"

# Gadgetbridge logcat 过滤 tag
GB_TAGS = [
    "Gadgetbridge",
    "XiaomiHealthService",
    "XiaomiActivitySync",
    "HuamiSupport",
    "MiBandSupport",
    "XiaomiSupport",
    "nodomain.freeyourgadget",
]


def post_update(data: dict):
    """POST 数据到后端。"""
    try:
        body = json.dumps(data).encode()
        req = Request(API_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as resp:
            pass
    except URLError as e:
        print(f"  ⚠ POST 失败: {e}")
    except Exception as e:
        print(f"  ⚠ 错误: {e}")


def parse_logcat_line(line: str) -> dict | None:
    """从 logcat 行中提取健康数据。"""
    # 心率相关模式
    hr_patterns = [
        r"heart.?rate.*?(\d{2,3})\s*bpm",
        r"Heart rate:\s*(\d{2,3})",
        r"heartRate[=:]\s*(\d{2,3})",
        r"HR[=:]\s*(\d{2,3})",
        r"realtime.*heart.*?(\d{2,3})",
    ]
    for pat in hr_patterns:
        m = re.search(pat, line, re.IGNORECASE)
        if m:
            bpm = int(m.group(1))
            if 30 <= bpm <= 220:
                return {"heart_rate": bpm}

    # 步数
    step_patterns = [
        r"steps[=:]\s*(\d+)",
        r"step.?count[=:]\s*(\d+)",
        r"totalSteps[=:]\s*(\d+)",
    ]
    for pat in step_patterns:
        m = re.search(pat, line, re.IGNORECASE)
        if m:
            steps = int(m.group(1))
            if 0 < steps < 200000:
                return {"steps": steps}

    # 电量
    bat_patterns = [
        r"battery[=:]\s*(\d{1,3})%?",
        r"batteryLevel[=:]\s*(\d{1,3})",
    ]
    for pat in bat_patterns:
        m = re.search(pat, line, re.IGNORECASE)
        if m:
            pct = int(m.group(1))
            if 0 <= pct <= 100:
                return {"battery": pct}

    # SpO2
    spo2_patterns = [
        r"spo2[=:]\s*(\d{2,3})",
        r"oxygen.*?(\d{2,3})%",
    ]
    for pat in spo2_patterns:
        m = re.search(pat, line, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if 70 <= val <= 100:
                return {"spo2": val}

    # 压力
    stress_patterns = [
        r"stress[=:]\s*(\d{1,3})",
    ]
    for pat in stress_patterns:
        m = re.search(pat, line, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if 0 < val <= 100:
                return {"stress": val}

    return None


def main():
    print(f"🔗 后端: {API_URL}")
    print(f"👤 用户: {USER_ID}")
    print(f"📱 监听 Gadgetbridge logcat…\n")

    # 清除旧 logcat 缓冲区
    subprocess.run(["adb", "logcat", "-c"], capture_output=True, timeout=5)

    # 启动 logcat 流
    proc = subprocess.Popen(
        ["adb", "logcat", "-v", "time"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    last_update: dict = {}
    update_count = 0

    try:
        for line in proc.stdout:
            line = line.strip()
            # 只处理 Gadgetbridge 相关行
            if not any(tag.lower() in line.lower() for tag in GB_TAGS):
                continue

            data = parse_logcat_line(line)
            if data and data != last_update:
                update_count += 1
                last_update = data
                print(f"  [{update_count}] {data}  ← {line[:80]}")
                post_update(data)

    except KeyboardInterrupt:
        print(f"\n\n✅ 结束。共转发 {update_count} 条更新。")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
