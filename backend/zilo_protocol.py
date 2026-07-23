"""Zilo 戒指（ring_sound）协议解析。纯函数、零依赖，方便单测与 JS 移植。

已实测命令表（来自官方测试 App 日志）：
  0x0101 -> 0x0102  获取系统信息 / 响应(firmware, battery)
  0x0601            开启事件/IMU 上报
  0x0605            六轴批量帧: seq区间, ~26帧/批, uptime(ms), accel(x,y,z), gyro(x,y,z)
  0x0701            触摸双击事件
  0x0702            动作手势事件
  0x0703            实体按钮双击事件  <- P0 确认信号
  0x0603            停止上报

帧格式假设：[cmd 2字节大端][body...]，无包头/校验。
若实测帧含包头或校验和，只需修改 parse_frame 的切片逻辑。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

CMD_SYS_INFO_REQ = 0x0101
CMD_SYS_INFO_RESP = 0x0102
CMD_REPORT_START = 0x0601
CMD_REPORT_STOP = 0x0603
CMD_IMU_BATCH = 0x0605
CMD_TOUCH_DOUBLE_TAP = 0x0701
CMD_MOTION_GESTURE = 0x0702
CMD_BUTTON_DOUBLE_PRESS = 0x0703


@dataclass
class ZiloFrame:
    cmd: int
    body: bytes


class FrameError(ValueError):
    pass


def parse_frame(data: bytes) -> ZiloFrame:
    if len(data) < 2:
        raise FrameError(f"帧过短: {data.hex()}")
    cmd = struct.unpack(">H", data[:2])[0]
    return ZiloFrame(cmd=cmd, body=data[2:])


def build_frame(cmd: int, body: bytes = b"") -> bytes:
    return struct.pack(">H", cmd) + body


def hex_to_bytes(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").replace("-", ""))


@dataclass
class ParsedIMU:
    seq_start: int
    seq_end: int
    uptime_ms: int
    accel: tuple[int, int, int]
    gyro: tuple[int, int, int]


def parse_imu_body(body: bytes) -> Optional[ParsedIMU]:
    """0x0605 body 解析。
    字段布局待实测确认（TODO: 用真实帧对照官方 App 显示值校准）。
    当前假设: seq_start(u16) seq_end(u16) uptime(u32) ax ay az gx gy gz (各 i16)，大端。
    长度不符时返回 None（上层记 warning，不崩溃）。"""
    if len(body) < 2 + 2 + 4 + 12:
        return None
    try:
        seq_start, seq_end = struct.unpack(">HH", body[0:4])
        uptime = struct.unpack(">I", body[4:8])[0]
        ax, ay, az, gx, gy, gz = struct.unpack(">6h", body[8:20])
        return ParsedIMU(seq_start, seq_end, uptime, (ax, ay, az), (gx, gy, gz))
    except struct.error:
        return None


def classify(frame: ZiloFrame) -> str:
    """帧 -> 语义事件名。未知命令返回 'unknown'（上层忽略，不抛错）。"""
    return {
        CMD_SYS_INFO_RESP: "sys_info",
        CMD_IMU_BATCH: "imu_batch",
        CMD_TOUCH_DOUBLE_TAP: "double_tap",
        CMD_MOTION_GESTURE: "motion_gesture",
        CMD_BUTTON_DOUBLE_PRESS: "double_press_confirm",
    }.get(frame.cmd, "unknown")
