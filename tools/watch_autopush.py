"""手机端·手表数据自动推送器（在 Termux 里跑）—— GB 后台导出 → 自动上传 Railway。

一次设置后全自动：你只看网页仪表盘，再也不用手动导出/上传。
它每隔 N 秒读一次 Gadgetbridge 的自动导出库，POST 到后端 /gadgetbridge-upload，
仪表盘随即刷新真实心率/血氧/步数/压力/睡眠。近实时（受 GB 导出间隔限制，约 30~60s）。

前置（都在手机上，一次性）：
  1. 装 Termux（F-Droid），`pkg install python && pip install httpx`
  2. Termux 授权访问存储：`termux-setup-storage`
  3. Gadgetbridge：设置 → 数据管理 → 打开「自动导出(Auto export)」，
     间隔设 1 分钟，记下导出路径（常见：/storage/emulated/0/Documents/Gadgetbridge/Gadgetbridge）

跑：
  python watch_autopush.py u_demo_01 \
    --backend https://redsignal-production.up.railway.app \
    --db /storage/emulated/0/Documents/Gadgetbridge/Gadgetbridge \
    --interval 30

不传 --db 时自动在常见路径里找。Ctrl-C 停。
"""
import argparse
import sys
import time
from pathlib import Path

import httpx

CANDIDATES = [
    "/storage/emulated/0/Documents/Gadgetbridge/Gadgetbridge",
    "/storage/emulated/0/Gadgetbridge/Gadgetbridge",
    "/storage/emulated/0/Download/Gadgetbridge",
    "/sdcard/Documents/Gadgetbridge/Gadgetbridge",
    "/sdcard/Gadgetbridge/Gadgetbridge",
]


def find_db(explicit: str | None) -> str | None:
    if explicit and Path(explicit).exists():
        return explicit
    if explicit:
        print(f"指定的 --db 不存在：{explicit}")
    # 自动探测：常见路径 + Documents/Gadgetbridge 目录里最新的文件
    for c in CANDIDATES:
        if Path(c).exists():
            return c
    for d in ("/storage/emulated/0/Documents/Gadgetbridge",
              "/storage/emulated/0/Gadgetbridge"):
        p = Path(d)
        if p.is_dir():
            files = sorted(p.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files:
                if f.is_file() and ("gadgetbridge" in f.name.lower()
                                    or f.suffix.lower() in (".sqlite", ".db")):
                    return str(f)
    return None


def push_once(backend: str, user_id: str, db: str) -> bool:
    try:
        with open(db, "rb") as f:
            r = httpx.post(f"{backend}/api/devices/{user_id}/gadgetbridge-upload",
                           files={"file": ("Gadgetbridge", f, "application/octet-stream")},
                           timeout=20)
        r.raise_for_status()
        w = r.json().get("watch", {})
        print(f"[{time.strftime('%H:%M:%S')}] ✓ 心率 {w.get('heart_rate_bpm')}bpm  "
              f"步 {w.get('today_steps')}  血氧 {w.get('spo2_percent')}%  "
              f"压力 {w.get('stress_level')}  睡眠 {w.get('sleep_hours')}h")
        return True
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] ✗ 上传失败：{e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("user_id")
    ap.add_argument("--backend", default="https://redsignal-production.up.railway.app")
    ap.add_argument("--db")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    db = find_db(args.db)
    if not db:
        print("没找到 Gadgetbridge 导出库。请在 GB 里开启自动导出后重试，")
        print("或用 --db 指定路径。已试过：")
        for c in CANDIDATES:
            print("  ", c)
        sys.exit(1)

    print(f"导出库：{db}")
    print(f"后端：{args.backend}   用户：{args.user_id}   间隔：{args.interval:.0f}s")
    print("GB 后台自动导出 + 本脚本自动推送 = 网页近实时出数据。Ctrl-C 停。\n")
    while True:
        push_once(args.backend, args.user_id, db)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
