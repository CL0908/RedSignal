"""从手机拉取 Gadgetbridge 数据库并解析小米手表健康数据存入本地。

用法:
  python3 tools/pull_watch_data.py

前提:
  1. 手机 USB 连接且 adb 已授权
  2. Gadgetbridge 已安装且已连接小米手表
  3. Gadgetbridge 设置中已开启「写入日志文件」（可选但推荐）

数据会存到 data/watch_health.json（供后端 API 读取）。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import gadgetbridge  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

GB_PACKAGE = "nodomain.freeyourgadget.gadgetbridge"
GB_DB_REMOTE = f"/sdcard/Android/data/{GB_PACKAGE}/files/Gadgetbridge"
GB_DB_LOCAL = DATA_DIR / "Gadgetbridge.sqlite"

# 备选路径（某些版本/ROM）
ALT_PATHS = [
    f"/sdcard/Android/data/{GB_PACKAGE}/files/Gadgetbridge",
    f"/data/data/{GB_PACKAGE}/databases/Gadgetbridge",
    f"/sdcard/Gadgetbridge/Gadgetbridge",
]


def adb(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=30)


def trigger_export():
    """通过 adb broadcast 触发 Gadgetbridge 导出数据库。"""
    print("📡 触发 Gadgetbridge 数据库导出…")
    r = adb("shell", "am", "broadcast",
            "-a", f"{GB_PACKAGE}.command.TRIGGER_EXPORT",
            "-p", GB_PACKAGE)
    print(f"   broadcast result: {r.stdout.strip()}")
    # 等一下导出完成
    import time
    time.sleep(3)


def find_and_pull_db() -> Path | None:
    """尝试多种路径拉取 Gadgetbridge 数据库。"""
    # 先触发导出
    trigger_export()

    # 查找导出文件
    print("🔍 查找 Gadgetbridge 数据库…")

    # 方法1: 查找导出目录下的文件
    r = adb("shell", "ls", f"/sdcard/Android/data/{GB_PACKAGE}/files/")
    if r.returncode == 0:
        print(f"   files目录: {r.stdout.strip()[:200]}")

    # 方法2: find 搜索
    r = adb("shell", "find", "/sdcard/", "-name", "Gadgetbridge*", "-type", "f",
            "2>/dev/null")
    if r.stdout.strip():
        print(f"   找到: {r.stdout.strip()[:300]}")
        # 取第一个 .sqlite 或无后缀的数据库文件
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if line and ("sqlite" in line.lower() or line.endswith("Gadgetbridge")):
                print(f"   ⬇ 拉取: {line}")
                pull = adb("pull", line, str(GB_DB_LOCAL))
                if pull.returncode == 0:
                    print(f"   ✓ 已保存到 {GB_DB_LOCAL}")
                    return GB_DB_LOCAL
                else:
                    print(f"   ✗ pull 失败: {pull.stderr.strip()}")

    # 方法3: 直接尝试已知路径
    for remote in ALT_PATHS:
        print(f"   尝试: {remote}")
        pull = adb("pull", remote, str(GB_DB_LOCAL))
        if pull.returncode == 0:
            print(f"   ✓ 拉取成功!")
            return GB_DB_LOCAL

    # 方法4: 用 run-as（需要 debug build 或 root）
    print("   尝试 run-as…")
    r = adb("shell", "run-as", GB_PACKAGE, "ls", "databases/")
    if r.returncode == 0 and "Gadgetbridge" in r.stdout:
        # 先 cp 到 sdcard 再 pull
        adb("shell", "run-as", GB_PACKAGE,
            "cp", "databases/Gadgetbridge", "/sdcard/Gadgetbridge_export.sqlite")
        pull = adb("pull", "/sdcard/Gadgetbridge_export.sqlite", str(GB_DB_LOCAL))
        if pull.returncode == 0:
            print(f"   ✓ 通过 run-as 拉取成功!")
            return GB_DB_LOCAL

    return None


def parse_and_store(db_path: Path, user_id: str = "u_demo_a") -> dict:
    """解析 Gadgetbridge DB 并存储为 JSON。"""
    print(f"\n📊 解析数据库: {db_path} ({db_path.stat().st_size / 1024:.0f} KB)")

    health = gadgetbridge.read_db(db_path)
    health.user_id = user_id

    # 构建输出
    result = {
        "user_id": user_id,
        "device_name": health.device_name,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "today_steps": health.today_steps,
        "sleep_hours": round(health.sleep_hours, 1),
        "last_heart_rate": {
            "bpm": health.last_heart_rate.bpm,
            "at": health.last_heart_rate.timestamp.isoformat(),
        } if health.last_heart_rate else None,
        "last_spo2": {
            "percent": health.last_spo2.spo2_percent,
            "at": health.last_spo2.timestamp.isoformat(),
        } if health.last_spo2 else None,
        "last_stress": {
            "level": health.last_stress.stress_level,
            "at": health.last_stress.timestamp.isoformat(),
        } if health.last_stress else None,
        "heart_rate_history": [
            {"bpm": s.bpm, "at": s.timestamp.isoformat()}
            for s in health.heart_rate_history[-100:]  # 最近100条
        ],
        "sleep_stages": [
            {"stage": s.stage, "at": s.timestamp.isoformat(), "min": s.duration_min}
            for s in health.sleep_stages
        ],
    }

    # 保存
    out_path = DATA_DIR / "watch_health.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 数据已保存到 {out_path}")
    print(f"   设备: {result['device_name']}")
    print(f"   今日步数: {result['today_steps']}")
    print(f"   睡眠: {result['sleep_hours']}h")
    if result['last_heart_rate']:
        print(f"   最新心率: {result['last_heart_rate']['bpm']} bpm")
    if result['last_spo2']:
        print(f"   血氧: {result['last_spo2']['percent']}%")
    if result['last_stress']:
        print(f"   压力: {result['last_stress']['level']}")
    print(f"   心率历史: {len(result['heart_rate_history'])} 条")

    return result


def main():
    # 检查 adb
    r = adb("devices")
    if r.returncode != 0:
        print("❌ adb 不可用"); sys.exit(1)

    lines = [l for l in r.stdout.strip().split("\n")[1:] if l.strip()]
    if not lines:
        print("❌ 没有连接的设备"); sys.exit(1)

    device_line = lines[0]
    if "unauthorized" in device_line:
        print("❌ 设备未授权！请在手机上点击「允许USB调试」")
        print(f"   设备: {device_line}")
        sys.exit(1)

    if "device" not in device_line:
        print(f"❌ 设备状态异常: {device_line}"); sys.exit(1)

    print(f"✓ 设备已连接: {device_line.split()[0]}")

    # 拉取数据库
    db_path = find_and_pull_db()
    if db_path is None:
        print("\n❌ 无法拉取 Gadgetbridge 数据库")
        print("   请确保:")
        print("   1. Gadgetbridge 已安装并连接手表")
        print("   2. 在 Gadgetbridge 设置中手动导出一次数据库")
        print("   3. 或者手动 adb pull 数据库文件到 data/Gadgetbridge.sqlite")
        sys.exit(1)

    # 解析并存储
    parse_and_store(db_path)


if __name__ == "__main__":
    main()
