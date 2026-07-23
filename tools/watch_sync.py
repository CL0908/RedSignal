"""小米手表 → 后端 的「近实时」桥。

原理：Gadgetbridge 把手表数据存在 SQLite；本脚本定时把那个库从手机拉到电脑，
调用后端 /api/devices/{user}/gadgetbridge-sync，仪表盘随即刷新出真实心率/步数/血氧等。

前置（在 Gadgetbridge 里开一次）：
  设置 → 数据管理 → 自动导出(Auto export) → 打开，间隔设 1 分钟，
  导出位置记下来（常见 /storage/emulated/0/Documents/Gadgetbridge/Gadgetbridge 或 .../Gadgetbridge/…）
  —— 自动导出的文件无需 root 就能 adb pull。

用法：
  # 自动：adb 找到并拉取导出库，循环同步（手机开 USB 调试并 adb 授权）
  .venv/bin/python -u tools/watch_sync.py u_demo_01

  # 手动：你已把导出库拷到电脑，直接循环喂后端（无需 adb）
  .venv/bin/python -u tools/watch_sync.py u_demo_01 --db /path/to/Gadgetbridge.sqlite

  # 只同步一次
  .venv/bin/python -u tools/watch_sync.py u_demo_01 --once

选项：--interval 秒(默认30) --backend http://localhost:8000 --remote <手机上的导出路径>
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

import httpx

LOCAL_PULL = "/tmp/redsignal_gb.sqlite"
# adb 常见导出/数据库位置（自动探测，取最新）
ADB_CANDIDATES = [
    "/storage/emulated/0/Documents/Gadgetbridge/Gadgetbridge",
    "/storage/emulated/0/Documents/Gadgetbridge",
    "/storage/emulated/0/Gadgetbridge/Gadgetbridge",
    "/storage/emulated/0/Download/Gadgetbridge",
    "/sdcard/Documents/Gadgetbridge/Gadgetbridge",
    "/sdcard/Gadgetbridge/Gadgetbridge",
]


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def adb_available() -> bool:
    rc, _ = _run(["adb", "get-state"])
    return rc == 0


def adb_find_export(explicit: str | None) -> str | None:
    """在手机上找 Gadgetbridge 导出库，返回远端路径。"""
    if explicit:
        return explicit
    # 用 find 搜 Gadgetbridge 导出文件（*.sqlite / 无扩展名的 Gadgetbridge），取最新
    rc, out = _run(["adb", "shell",
                    "ls -t /storage/emulated/0/Documents/Gadgetbridge/ "
                    "/storage/emulated/0/Gadgetbridge/ 2>/dev/null"])
    if rc == 0 and out:
        for line in out.splitlines():
            name = line.strip()
            if name and ("gadgetbridge" in name.lower() or name.lower().endswith(".sqlite")):
                # 组合成完整路径（尝试两个目录）
                for base in ("/storage/emulated/0/Documents/Gadgetbridge/",
                             "/storage/emulated/0/Gadgetbridge/"):
                    rc2, _ = _run(["adb", "shell", f"test -f '{base}{name}' && echo ok"])
                    if rc2 == 0:
                        return base + name
    # 兜底：逐个候选
    for c in ADB_CANDIDATES:
        rc, _ = _run(["adb", "shell", f"test -f '{c}' && echo ok"])
        if rc == 0:
            return c
    return None


def adb_pull(remote: str) -> str | None:
    rc, out = _run(["adb", "pull", remote, LOCAL_PULL])
    if rc == 0 and Path(LOCAL_PULL).exists():
        return LOCAL_PULL
    print(f"  adb pull 失败: {out}")
    return None


def sync_once(backend: str, user_id: str, db_path: str) -> bool:
    """让后端读取该库并更新手表数据；打印读到的关键值。"""
    try:
        r = httpx.post(f"{backend}/api/devices/{user_id}/gadgetbridge-sync",
                       json={"db_path": db_path}, timeout=15)
        r.raise_for_status()
        w = r.json().get("watch", {})
        print(f"  ✓ 已同步 → 心率 {w.get('heart_rate_bpm')}bpm  步数 {w.get('today_steps')}  "
              f"血氧 {w.get('spo2_percent')}%  压力 {w.get('stress_level')}  "
              f"睡眠 {w.get('sleep_hours')}h  在线={w.get('connected')}")
        return True
    except Exception as e:
        print(f"  ✗ 同步失败: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("user_id")
    ap.add_argument("--db", help="本地已有的 Gadgetbridge 库路径（跳过 adb）")
    ap.add_argument("--remote", help="手机上导出库的明确路径")
    ap.add_argument("--backend", default="http://localhost:8000")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    use_adb = args.db is None
    if use_adb and not adb_available():
        print("没检测到 adb 设备。要么开手机 USB 调试并 `adb devices` 授权，")
        print("要么用 --db 指向你已拷到电脑的 Gadgetbridge 导出库。")
        sys.exit(1)

    remote = None
    if use_adb:
        remote = adb_find_export(args.remote)
        if not remote:
            print("adb 连上了，但没找到 Gadgetbridge 导出库。")
            print("请在 Gadgetbridge 里：数据管理 → 导出数据库(或开自动导出) 后重试，")
            print("或用 --remote 指定手机上的导出路径。")
            sys.exit(1)
        print(f"手机导出库: {remote}")

    print(f"后端: {args.backend}   用户: {args.user_id}   间隔: {args.interval:.0f}s\n")
    while True:
        db = args.db
        if use_adb:
            db = adb_pull(remote)
        if db:
            print(f"[{time.strftime('%H:%M:%S')}] 读取 {db}")
            sync_once(args.backend, args.user_id, db)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
