"""手表数据持久化存储 —— 把 Gadgetbridge 解析出的全部数据写进 SQLite，重启不丢。

存两样：
  1. 时间序列 + 快照写进本地 SQLite（hr_samples / sleep_stages / snapshots）。
  2. 原始 GB 导出库整份留档到 data/watch_raw/（要「全部数据」时可直接下载原库）。

纯标准库 sqlite3，零额外依赖。DB 路径可用 WATCH_DB_PATH 覆盖。
注：Railway 文件系统重部署会重置——要真正长期持久需挂 volume 或换 Postgres。
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__import__("os").environ.get("WATCH_DB_PATH", "redsignal_watch.db"))
RAW_DIR = Path("data/watch_raw")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS hr_samples(
            user_id TEXT, ts INTEGER, bpm INTEGER,
            PRIMARY KEY(user_id, ts));
        CREATE TABLE IF NOT EXISTS sleep_stages(
            user_id TEXT, ts INTEGER, stage TEXT, duration_min INTEGER,
            PRIMARY KEY(user_id, ts));
        CREATE TABLE IF NOT EXISTS snapshots(
            user_id TEXT, ts INTEGER, device_name TEXT,
            last_hr INTEGER, today_steps INTEGER, spo2 INTEGER,
            stress INTEGER, sleep_hours REAL);
        CREATE TABLE IF NOT EXISTS raw_uploads(
            user_id TEXT, ts INTEGER, path TEXT, bytes INTEGER);
        """)


def store_snapshot(user_id: str, health) -> dict:
    """把一份 WatchHealthSnapshot 的全部时序 + 快照写库。返回本次写入统计。"""
    init()
    now = int(time.time())
    hr_rows = [(user_id, int(s.timestamp.timestamp()), int(s.bpm))
               for s in (health.heart_rate_history or [])]
    sleep_rows = [(user_id, int(s.timestamp.timestamp()), s.stage, int(s.duration_min))
                  for s in (health.sleep_stages or [])]
    with _conn() as c:
        c.executemany("INSERT OR IGNORE INTO hr_samples VALUES(?,?,?)", hr_rows)
        c.executemany("INSERT OR IGNORE INTO sleep_stages VALUES(?,?,?,?)", sleep_rows)
        c.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)", (
            user_id, now, health.device_name,
            health.last_heart_rate.bpm if health.last_heart_rate else None,
            health.today_steps,
            health.last_spo2.spo2_percent if health.last_spo2 else None,
            health.last_stress.stress_level if health.last_stress else None,
            round(health.sleep_hours, 2),
        ))
    return {"hr_inserted": len(hr_rows), "sleep_inserted": len(sleep_rows)}


def store_raw(user_id: str, src_path: Path) -> str:
    """把原始 GB 库整份留档，返回留档路径。"""
    init()
    dst = RAW_DIR / f"{user_id}_{int(time.time())}.sqlite"
    shutil.copyfile(src_path, dst)
    with _conn() as c:
        c.execute("INSERT INTO raw_uploads VALUES(?,?,?,?)",
                  (user_id, int(time.time()), str(dst), dst.stat().st_size))
    return str(dst)


def dump(user_id: str, limit: int = 500) -> dict:
    """导出某用户已存的全部数据（计数 + 最近样本 + 最新快照）。"""
    init()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        hr_total = c.execute("SELECT COUNT(*) FROM hr_samples WHERE user_id=?", (user_id,)).fetchone()[0]
        sleep_total = c.execute("SELECT COUNT(*) FROM sleep_stages WHERE user_id=?", (user_id,)).fetchone()[0]
        hr = [dict(r) for r in c.execute(
            "SELECT ts,bpm FROM hr_samples WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit)).fetchall()]
        sleep = [dict(r) for r in c.execute(
            "SELECT ts,stage,duration_min FROM sleep_stages WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit)).fetchall()]
        snap = c.execute(
            "SELECT * FROM snapshots WHERE user_id=? ORDER BY ts DESC LIMIT 1", (user_id,)).fetchone()
        raws = [dict(r) for r in c.execute(
            "SELECT ts,path,bytes FROM raw_uploads WHERE user_id=? ORDER BY ts DESC", (user_id,)).fetchall()]
    return {
        "user_id": user_id,
        "hr_total": hr_total, "sleep_total": sleep_total,
        "latest_snapshot": dict(snap) if snap else None,
        "hr_recent": hr, "sleep_recent": sleep,
        "raw_uploads": raws,
    }
