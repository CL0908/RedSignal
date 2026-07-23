"""Zilo 协议解析测试。"""
import struct

import pytest

from backend import zilo_protocol as zp


def test_parse_button_double_press():
    frame = zp.parse_frame(bytes.fromhex("0703"))
    assert frame.cmd == zp.CMD_BUTTON_DOUBLE_PRESS
    assert zp.classify(frame) == "double_press_confirm"


def test_parse_touch_and_gesture():
    assert zp.classify(zp.parse_frame(bytes.fromhex("0701"))) == "double_tap"
    assert zp.classify(zp.parse_frame(bytes.fromhex("0702"))) == "motion_gesture"


def test_unknown_command_does_not_crash():
    frame = zp.parse_frame(bytes.fromhex("ffff00"))
    assert zp.classify(frame) == "unknown"


def test_too_short_frame_raises():
    with pytest.raises(zp.FrameError):
        zp.parse_frame(b"\x01")


def test_build_frame_roundtrip():
    raw = zp.build_frame(zp.CMD_REPORT_START)
    assert raw.hex() == "0601"
    frame = zp.parse_frame(raw)
    assert frame.cmd == 0x0601 and frame.body == b""


def test_hex_with_spaces_and_dashes():
    assert zp.hex_to_bytes("07 03") == b"\x07\x03"
    assert zp.hex_to_bytes("07-03") == b"\x07\x03"


def test_imu_body_parse():
    body = struct.pack(">HHI6h", 4004, 4029, 4775550, -2017, 90, 29, 285, 395, 0)
    parsed = zp.parse_imu_body(body)
    assert parsed is not None
    assert (parsed.seq_start, parsed.seq_end) == (4004, 4029)
    assert parsed.uptime_ms == 4775550
    assert parsed.accel == (-2017, 90, 29)
    assert parsed.gyro == (285, 395, 0)


def test_imu_body_too_short_returns_none():
    assert zp.parse_imu_body(b"\x00\x01") is None
