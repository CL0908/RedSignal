"""Tests for wearable_hub and gadgetbridge integration."""
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.gadgetbridge import (
    HeartRateSample,
    WatchHealthSnapshot,
    read_db,
)
from backend.wearable_hub import WearableHub


# ---- Gadgetbridge DB reader tests ----

def _create_test_db(path: Path) -> None:
    """Create a minimal Gadgetbridge-like SQLite DB for testing."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE DEVICE (
            _id INTEGER PRIMARY KEY,
            NAME TEXT,
            IDENTIFIER TEXT,
            TYPE INTEGER
        )
    """)
    conn.execute("INSERT INTO DEVICE VALUES (1, 'Xiaomi Watch S1', 'AA:BB:CC:DD:EE:FF', 1)")

    conn.execute("""
        CREATE TABLE XIAOMI_ACTIVITY_SAMPLE (
            TIMESTAMP INTEGER,
            DEVICE_ID INTEGER,
            HEART_RATE INTEGER,
            STEPS INTEGER,
            RAW_KIND INTEGER,
            RAW_INTENSITY INTEGER
        )
    """)
    # Insert some test data - recent heart rate and steps
    now_ts = int(datetime.now(timezone.utc).timestamp())
    for i in range(10):
        conn.execute(
            "INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES (?, 1, ?, ?, 0, 50)",
            (now_ts - (10 - i) * 60, 72 + i, 100 + i * 10),
        )

    conn.commit()
    conn.close()


def test_read_db_missing_file():
    snap = read_db("/nonexistent/path.sqlite")
    assert snap.user_id == "unknown"
    assert snap.today_steps == 0


def test_read_db_basic():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        _create_test_db(db_path)
        snap = read_db(db_path)
        assert snap.device_name == "Xiaomi Watch S1"
        assert snap.last_heart_rate is not None
        assert snap.last_heart_rate.bpm == 81  # 72 + 9 (last sample)
        assert snap.today_steps > 0
        assert len(snap.heart_rate_history) == 10
    finally:
        db_path.unlink(missing_ok=True)


def test_read_db_with_device_filter():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        _create_test_db(db_path)
        snap = read_db(db_path, device_address="AA:BB:CC:DD:EE:FF")
        assert snap.device_name == "Xiaomi Watch S1"
    finally:
        db_path.unlink(missing_ok=True)


# ---- WearableHub tests ----

def test_hub_ring_lifecycle():
    hub = WearableHub()
    snap = hub.get("user1")
    assert not snap.ring.connected

    hub.ring_connected("user1", firmware="V2.0", battery=96)
    snap = hub.get("user1")
    assert snap.ring.connected
    assert snap.ring.firmware == "V2.0"
    assert snap.ring.battery_percent == 96

    hub.ring_button_press("user1")
    assert snap.ring.last_button_press is not None

    hub.ring_gesture("user1", "wave")
    assert snap.ring.last_gesture == "wave"

    hub.ring_imu("user1", (100.0, -50.0, 980.0), (10.0, -5.0, 2.0))
    assert snap.ring.imu_active
    assert snap.ring.last_accel == (100.0, -50.0, 980.0)

    hub.ring_disconnected("user1")
    assert not snap.ring.connected
    assert not snap.ring.imu_active


def test_hub_watch_realtime():
    hub = WearableHub()

    hub.watch_realtime_hr("user1", 75)
    snap = hub.get("user1")
    assert snap.watch.connected
    assert snap.watch.heart_rate_bpm == 75

    hub.watch_realtime_steps("user1", 5432)
    assert snap.watch.today_steps == 5432

    hub.watch_disconnected("user1")
    assert not snap.watch.connected


def test_hub_watch_sync():
    hub = WearableHub()

    health = WatchHealthSnapshot(
        user_id="user1",
        device_name="Mi Band 7",
        today_steps=8000,
        sleep_hours=7.5,
        last_heart_rate=HeartRateSample(
            timestamp=datetime.now(timezone.utc), bpm=68),
    )
    hub.watch_sync("user1", health)

    snap = hub.get("user1")
    assert snap.watch.device_name == "Mi Band 7"
    assert snap.watch.today_steps == 8000
    assert snap.watch.heart_rate_bpm == 68
    assert snap.watch.sleep_hours == 7.5


def test_hub_combined_snapshot():
    hub = WearableHub()

    hub.ring_connected("user1", firmware="V2.0", battery=80)
    hub.watch_realtime_hr("user1", 72)
    hub.watch_realtime_steps("user1", 3000)

    result = hub.get("user1").to_dict()
    assert result["ring"]["connected"] is True
    assert result["ring"]["battery_percent"] == 80
    assert result["watch"]["connected"] is True
    assert result["watch"]["heart_rate_bpm"] == 72
    assert result["watch"]["today_steps"] == 3000
    assert result["user_id"] == "user1"
