"""Zilo 协议解析测试（真实帧格式：magic 0x3F + ver + cmd + len + CRC16 + body）。

用真机抓包帧回归 + APK codec 校验。旧版「2 字节大端」假设已废弃。
"""
import struct

import pytest

from backend import zilo_protocol as zp


def test_parse_button_double_press():
    frame = zp.parse_frame(zp.build_frame(zp.CMD_BUTTON_DOUBLE_PRESS))
    assert frame.cmd == zp.CMD_BUTTON_DOUBLE_PRESS
    assert zp.classify(frame) == "double_press_confirm"


def test_parse_touch_and_gesture():
    assert zp.classify(zp.parse_frame(zp.build_frame(0x0701))) == "double_tap"
    assert zp.classify(zp.parse_frame(zp.build_frame(0x0702))) == "motion_gesture"


def test_unknown_command_does_not_crash():
    frame = zp.parse_frame(zp.build_frame(0x0999, b"\x01\x02"))
    assert zp.classify(frame) == "unknown"


def test_too_short_frame_raises():
    with pytest.raises(zp.FrameError):
        zp.parse_frame(b"\x01")


def test_bad_magic_raises():
    with pytest.raises(zp.FrameError):
        zp.parse_frame(b"\x00\x00\x04\x07\x03\x00\x00\x00\x00\xff\xff")


def test_build_frame_matches_apk_codec():
    # 0x0601 开六轴（空 payload）；空 payload 的 CRC16 = 0xFFFF
    assert zp.build_frame(zp.CMD_REPORT_START).hex() == "3f0004060100000000ffff"
    assert zp.crc16(b"") == 0xFFFF
    frame = zp.parse_frame(zp.build_frame(zp.CMD_REPORT_START))
    assert frame.cmd == 0x0601 and frame.body == b"" and frame.crc_valid


def test_hex_with_spaces_and_dashes():
    assert zp.hex_to_bytes("07 03") == b"\x07\x03"
    assert zp.hex_to_bytes("07-03") == b"\x07\x03"


# --- 真机抓包帧回归（2026-07-23，0x0401 校时请求）---
REAL_CAPTURED_FRAMES = [
    ("3f0004040100000004ee010000ac9b", 0x0401, "0000ac9b"),
    ("3f0004040100000004451b0000acc4", 0x0401, "0000acc4"),
    ("3f0004040100000004909600" + "00aceb", 0x0401, "0000aceb"),
]


def test_real_captured_frames_decode_and_crc_valid():
    for hexstr, exp_cmd, exp_body in REAL_CAPTURED_FRAMES:
        frame = zp.parse_frame(zp.hex_to_bytes(hexstr))
        assert frame.cmd == exp_cmd
        assert frame.body.hex() == exp_body
        assert frame.crc_valid
        assert zp.classify(frame) == "time_sync_req"


def test_time_sync_ack_roundtrip():
    frame = zp.parse_frame(zp.build_time_sync_ack(1_700_000_000))
    assert frame.cmd == zp.CMD_TIME_SYNC_ACK
    assert frame.crc_valid
    assert struct.unpack(">I", frame.body)[0] == 1_700_000_000


def test_imu_body_parse():
    # 真机布局：err(u16) seqStart(u32) frameCount(u16) frameSize(u16) 然后 16B/帧
    body = struct.pack(">HIHH", 0, 4004, 2, 16)
    body += struct.pack(">Ihhhhhh", 4775550, -2017, 90, 29, 285, 395, 0)
    body += struct.pack(">Ihhhhhh", 4775560, 1, 2, 3, 4, 5, 6)
    parsed = zp.parse_imu_body(body)
    assert parsed is not None
    assert parsed.seq_start == 4004 and parsed.seq_end == 4005
    assert parsed.uptime_ms == 4775550
    assert parsed.accel == (-2017, 90, 29)
    assert parsed.gyro == (285, 395, 0)


def test_imu_body_too_short_returns_none():
    assert zp.parse_imu_body(b"\x00\x01") is None


def test_sys_info_extracts_battery_and_firmware():
    def s(x: bytes) -> bytes:
        return struct.pack(">H", len(x)) + x
    body = struct.pack(">H", 0) + s(b"V2.000.0001.0015") + struct.pack(">I", 1700000000)
    body += struct.pack(">II", 16_777_216, 16_707_584) + struct.pack(">H", 96) + bytes([0])
    body += s(b"unknown") + s(b"F7BD14782E29FA21") + s(b"ring_sound")
    info = zp.parse_sys_info(body)
    assert info["firmwareVersion"] == "V2.000.0001.0015"
    assert info["batteryPercent"] == 96
    assert info["batteryCharging"] is False
    assert info["model"] == "ring_sound"
    assert info["cpuId"] == "F7BD14782E29FA21"
