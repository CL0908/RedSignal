"""Gadgetbridge SQLite 数据库读取器 —— 从导出的 DB 中提取小米手表健康数据。

Gadgetbridge 可通过 Intent 或设置页面导出 SQLite 数据库：
  路径通常为: /sdcard/Android/data/nodomain.freeyourgadget.gadgetbridge/files/
  文件名: Gadgetbridge 或 Gadgetbridge_YYYY-MM-DD.sqlite

数据表（小米/Amazfit 系列）:
  - XIAOMI_ACTIVITY_SAMPLE: timestamp, steps, heart_rate, raw_kind, raw_intensity
  - XIAOMI_SLEEP_STAGE_SAMPLE: timestamp, stage (deep/light/rem/awake)
  - XIAOMI_SLEEP_TIME_SAMPLE: total_duration, wake_time, etc.
  - HUAMI_EXTENDED_ACTIVITY_SAMPLE: spo2, stress, heart_rate (部分设备)

也兼容旧 Huami 格式:
  - MI_BAND_ACTIVITY_SAMPLE: 早期小米手环格式

本模块只读取、不修改数据库。纯函数，不依赖 FastAPI。
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("gadgetbridge")


@dataclass
class HeartRateSample:
    timestamp: datetime
    bpm: int
    source: str = "xiaomi_watch"


@dataclass
class StepsSample:
    timestamp: datetime
    steps: int
    source: str = "xiaomi_watch"


@dataclass
class SleepSample:
    timestamp: datetime
    stage: str   # deep | light | rem | awake
    duration_min: int = 0
    source: str = "xiaomi_watch"


@dataclass
class SpO2Sample:
    timestamp: datetime
    spo2_percent: int
    source: str = "xiaomi_watch"


@dataclass
class StressSample:
    timestamp: datetime
    stress_level: int  # 0-100
    source: str = "xiaomi_watch"


@dataclass
class WatchHealthSnapshot:
    """某时刻小米手表健康数据快照。"""
    user_id: str
    device_name: str = "Xiaomi Watch"
    last_heart_rate: Optional[HeartRateSample] = None
    today_steps: int = 0
    last_spo2: Optional[SpO2Sample] = None
    last_stress: Optional[StressSample] = None
    sleep_hours: float = 0.0
    sleep_stages: list[SleepSample] = field(default_factory=list)
    heart_rate_history: list[HeartRateSample] = field(default_factory=list)
    battery_percent: int = -1
    updated_at: Optional[datetime] = None


def _ts_to_dt(ts: int) -> datetime:
    """Gadgetbridge 存储 Unix 秒时间戳。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _today_start_ts() -> int:
    """今日 00:00 UTC 的 Unix 秒。"""
    now = datetime.now(timezone.utc)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def read_db(db_path: str | Path, device_address: Optional[str] = None) -> WatchHealthSnapshot:
    """读取 Gadgetbridge 导出的 SQLite，返回健康快照。

    Args:
        db_path: .sqlite 文件路径
        device_address: 可选，按设备 MAC 过滤（格式 XX:XX:XX:XX:XX:XX）

    Returns:
        WatchHealthSnapshot 包含最新心率、今日步数、睡眠、SpO2 等
    """
    db_path = Path(db_path)
    if not db_path.exists():
        log.warning("Gadgetbridge DB not found: %s", db_path)
        return WatchHealthSnapshot(user_id="unknown")

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = WatchHealthSnapshot(user_id="unknown")
        snapshot.updated_at = datetime.now(timezone.utc)

        # 检测可用表
        tables = _get_tables(conn)

        # 读取设备名
        device_name = _get_device_name(conn, device_address)
        if device_name:
            snapshot.device_name = device_name

        # 读心率
        snapshot.heart_rate_history = _read_heart_rate(conn, tables, device_address)
        if snapshot.heart_rate_history:
            snapshot.last_heart_rate = snapshot.heart_rate_history[-1]

        # 读今日步数
        snapshot.today_steps = _read_today_steps(conn, tables, device_address)

        # 读睡眠
        snapshot.sleep_stages = _read_sleep(conn, tables, device_address)
        snapshot.sleep_hours = sum(s.duration_min for s in snapshot.sleep_stages) / 60.0

        # 读 SpO2
        snapshot.last_spo2 = _read_spo2(conn, tables, device_address)

        # 读压力
        snapshot.last_stress = _read_stress(conn, tables, device_address)

        # 读电量
        snapshot.battery_percent = _read_battery(conn, tables)

        return snapshot
    finally:
        conn.close()


def _get_tables(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in cur.fetchall()}


def _get_device_name(conn: sqlite3.Connection, address: Optional[str]) -> Optional[str]:
    try:
        if address:
            cur = conn.execute(
                "SELECT NAME FROM DEVICE WHERE IDENTIFIER = ? LIMIT 1", (address,))
            row = cur.fetchone()
            if row:
                return row[0]
        # 不再按 TYPE 过滤（小米手环 TYPE=0）：取第一条有名字的设备
        cur = conn.execute("SELECT NAME FROM DEVICE WHERE NAME IS NOT NULL LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def _read_battery(conn: sqlite3.Connection, tables: set[str]) -> int:
    """读最新电量（BATTERY_LEVEL.LEVEL）。无则 -1。"""
    if "BATTERY_LEVEL" not in tables:
        return -1
    try:
        cur = conn.execute(
            "SELECT LEVEL FROM BATTERY_LEVEL ORDER BY TIMESTAMP DESC LIMIT 1")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else -1
    except sqlite3.OperationalError:
        return -1


def _read_heart_rate(
    conn: sqlite3.Connection, tables: set[str], address: Optional[str]
) -> list[HeartRateSample]:
    """读取最近 24h 心率，返回按时间排序的样本列表。"""
    samples: list[HeartRateSample] = []
    since = int(datetime.now(timezone.utc).timestamp()) - 86400

    # 尝试多种表格式
    table_candidates = [
        "XIAOMI_ACTIVITY_SAMPLE",
        "MI_BAND_ACTIVITY_SAMPLE",
        "HUAMI_EXTENDED_ACTIVITY_SAMPLE",
    ]

    for table in table_candidates:
        if table not in tables:
            continue
        try:
            query = f"""
                SELECT TIMESTAMP, HEART_RATE FROM {table}
                WHERE HEART_RATE > 0 AND HEART_RATE < 255
                  AND TIMESTAMP > ?
                ORDER BY TIMESTAMP ASC
                LIMIT 1440
            """
            cur = conn.execute(query, (since,))
            for row in cur:
                samples.append(HeartRateSample(
                    timestamp=_ts_to_dt(row[0]),
                    bpm=row[1],
                ))
            if samples:
                break
        except sqlite3.OperationalError:
            continue

    return samples


def _read_today_steps(
    conn: sqlite3.Connection, tables: set[str], address: Optional[str]
) -> int:
    """读取今日累计步数。"""
    today_ts = _today_start_ts()

    table_candidates = [
        "XIAOMI_ACTIVITY_SAMPLE",
        "HUAMI_EXTENDED_ACTIVITY_SAMPLE",   # 小米手环 7 等 Huami 系列步数在此
        "MI_BAND_ACTIVITY_SAMPLE",
    ]

    for table in table_candidates:
        if table not in tables:
            continue
        try:
            query = f"""
                SELECT COALESCE(SUM(STEPS), 0) FROM {table}
                WHERE TIMESTAMP >= ? AND STEPS > 0
            """
            cur = conn.execute(query, (today_ts,))
            row = cur.fetchone()
            if row and row[0] > 0:
                return row[0]
        except sqlite3.OperationalError:
            continue
    return 0


def _read_sleep(
    conn: sqlite3.Connection, tables: set[str], address: Optional[str]
) -> list[SleepSample]:
    """读取最近一晚睡眠分段。"""
    samples: list[SleepSample] = []
    # 昨天 18:00 到现在
    since = int(datetime.now(timezone.utc).timestamp()) - 43200

    # 小米专用睡眠表
    if "XIAOMI_SLEEP_STAGE_SAMPLE" in tables:
        try:
            cur = conn.execute("""
                SELECT TIMESTAMP, STAGE FROM XIAOMI_SLEEP_STAGE_SAMPLE
                WHERE TIMESTAMP > ?
                ORDER BY TIMESTAMP ASC
            """, (since,))
            stage_map = {1: "light", 2: "deep", 3: "rem", 0: "awake"}
            for row in cur:
                samples.append(SleepSample(
                    timestamp=_ts_to_dt(row[0]),
                    stage=stage_map.get(row[1], "unknown"),
                    duration_min=1,  # 每条记录约 1 分钟粒度
                ))
            return samples
        except sqlite3.OperationalError:
            pass

    # Fallback: 从 activity sample 的 raw_kind 推断
    for table in ["XIAOMI_ACTIVITY_SAMPLE", "MI_BAND_ACTIVITY_SAMPLE"]:
        if table not in tables:
            continue
        try:
            cur = conn.execute(f"""
                SELECT TIMESTAMP, RAW_KIND FROM {table}
                WHERE TIMESTAMP > ? AND RAW_KIND IN (112, 121, 122, 123)
                ORDER BY TIMESTAMP ASC
            """, (since,))
            # raw_kind: 112=light, 121=deep, 122=rem, 123=awake (varies by firmware)
            kind_map = {112: "light", 121: "deep", 122: "rem", 123: "awake"}
            for row in cur:
                samples.append(SleepSample(
                    timestamp=_ts_to_dt(row[0]),
                    stage=kind_map.get(row[1], "unknown"),
                    duration_min=1,
                ))
            if samples:
                return samples
        except sqlite3.OperationalError:
            continue
    return samples


def _read_spo2(
    conn: sqlite3.Connection, tables: set[str], address: Optional[str]
) -> Optional[SpO2Sample]:
    """读取最新 SpO2。"""
    if "HUAMI_EXTENDED_ACTIVITY_SAMPLE" in tables:
        try:
            cur = conn.execute("""
                SELECT TIMESTAMP, SPO2 FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE
                WHERE SPO2 > 0 AND SPO2 <= 100
                ORDER BY TIMESTAMP DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return SpO2Sample(timestamp=_ts_to_dt(row[0]), spo2_percent=row[1])
        except sqlite3.OperationalError:
            pass

    # Xiaomi SPO2 表
    if "XIAOMI_SPO2_SAMPLE" in tables:
        try:
            cur = conn.execute("""
                SELECT TIMESTAMP, SPO2 FROM XIAOMI_SPO2_SAMPLE
                WHERE SPO2 > 0 AND SPO2 <= 100
                ORDER BY TIMESTAMP DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return SpO2Sample(timestamp=_ts_to_dt(row[0]), spo2_percent=row[1])
        except sqlite3.OperationalError:
            pass
    return None


def _read_stress(
    conn: sqlite3.Connection, tables: set[str], address: Optional[str]
) -> Optional[StressSample]:
    """读取最新压力值。"""
    if "HUAMI_EXTENDED_ACTIVITY_SAMPLE" in tables:
        try:
            cur = conn.execute("""
                SELECT TIMESTAMP, STRESS FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE
                WHERE STRESS > 0 AND STRESS <= 100
                ORDER BY TIMESTAMP DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return StressSample(timestamp=_ts_to_dt(row[0]), stress_level=row[1])
        except sqlite3.OperationalError:
            pass

    if "XIAOMI_STRESS_SAMPLE" in tables:
        try:
            cur = conn.execute("""
                SELECT TIMESTAMP, STRESS FROM XIAOMI_STRESS_SAMPLE
                WHERE STRESS > 0 AND STRESS <= 100
                ORDER BY TIMESTAMP DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return StressSample(timestamp=_ts_to_dt(row[0]), stress_level=row[1])
        except sqlite3.OperationalError:
            pass
    return None
