"""Gadgetbridge 小米/Redmi 库读取回归测试（合成 DB，不需真机）。"""
import sqlite3
import time

from backend import gadgetbridge


def _make_db(path):
    c = sqlite3.connect(str(path))
    c.executescript("""
        CREATE TABLE DEVICE(NAME TEXT, IDENTIFIER TEXT, TYPE INT);
        INSERT INTO DEVICE VALUES('Redmi Watch 5','AA:BB:CC:DD:EE:FF',30);
        CREATE TABLE XIAOMI_ACTIVITY_SAMPLE(TIMESTAMP INT, HEART_RATE INT, STEPS INT, RAW_KIND INT);
        CREATE TABLE HUAMI_EXTENDED_ACTIVITY_SAMPLE(TIMESTAMP INT, SPO2 INT, STRESS INT, HEART_RATE INT);
        CREATE TABLE XIAOMI_SLEEP_STAGE_SAMPLE(TIMESTAMP INT, STAGE INT);
    """)
    now = int(time.time())
    t0 = now - now % 86400  # 今日 00:00
    for i in range(20):
        c.execute("INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES(?,?,?,0)",
                  (t0 + i * 300, 70 + i % 15, 400 + i))
    c.execute("INSERT INTO HUAMI_EXTENDED_ACTIVITY_SAMPLE VALUES(?,?,?,?)",
              (now - 60, 97, 35, 76))
    # 睡眠：昨晚几段（stage_map: 1 light,2 deep,3 rem,0 awake）
    for j, stage in enumerate([2, 1, 3, 1, 0]):
        c.execute("INSERT INTO XIAOMI_SLEEP_STAGE_SAMPLE VALUES(?,?)",
                  (now - 40000 + j * 60, stage))
    c.commit()
    c.close()


def test_read_xiaomi_watch_db(tmp_path):
    db = tmp_path / "Gadgetbridge.sqlite"
    _make_db(db)
    snap = gadgetbridge.read_db(db)
    assert snap.device_name == "Redmi Watch 5"
    assert snap.last_heart_rate is not None and snap.last_heart_rate.bpm > 0
    assert snap.today_steps > 0
    assert snap.last_spo2 is not None and snap.last_spo2.spo2_percent == 97
    assert snap.last_stress is not None and snap.last_stress.stress_level == 35
    assert len(snap.sleep_stages) == 5


def test_read_xiaomi_mi_band_7_huami_extended(tmp_path):
    """小米手环7：步数在 HUAMI_EXTENDED、电量在 BATTERY_LEVEL、设备 TYPE=0、HR=255 无效。"""
    db = tmp_path / "Gadgetbridge"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE DEVICE(NAME TEXT, IDENTIFIER TEXT, TYPE INT);
        INSERT INTO DEVICE VALUES('Xiaomi Smart Band 7','E4:FF:21:00:0D:68',0);
        CREATE TABLE HUAMI_EXTENDED_ACTIVITY_SAMPLE(
            TIMESTAMP INT, DEVICE_ID INT, USER_ID INT, RAW_INTENSITY INT,
            STEPS INT, RAW_KIND INT, HEART_RATE INT, UNKNOWN1 INT,
            SLEEP INT, DEEP_SLEEP INT, REM_SLEEP INT);
        CREATE TABLE BATTERY_LEVEL(TIMESTAMP INT, DEVICE_ID INT, LEVEL INT, BATTERY_INDEX INT);
    """)
    now = int(time.time()); t0 = now - now % 86400
    for i in range(10):
        c.execute("INSERT INTO HUAMI_EXTENDED_ACTIVITY_SAMPLE VALUES(?,1,1,30,?,118,255,5,0,128,0)",
                  (t0 + i * 60, i))  # STEPS=i, HR=255(无效)
    c.execute("INSERT INTO BATTERY_LEVEL VALUES(?,1,31,0)", (now - 10,))
    c.commit(); c.close()

    snap = gadgetbridge.read_db(db)
    assert snap.device_name == "Xiaomi Smart Band 7"   # TYPE=0 也能取到名字
    assert snap.battery_percent == 31                   # 读到电量
    assert snap.today_steps == sum(range(10))           # 步数从 HUAMI_EXTENDED 汇总
    assert snap.last_heart_rate is None                 # HR 全 255 被正确过滤


def test_read_missing_db_returns_empty(tmp_path):
    snap = gadgetbridge.read_db(tmp_path / "nope.sqlite")
    assert snap.today_steps == 0
    assert snap.last_heart_rate is None
