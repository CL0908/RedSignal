"""Zilo 戒指（广播名 "ring" / model=ring_sound）BLE 透传协议解析。纯函数、零依赖。

⚠️ 帧格式已按**真机 + 官方 APK 逆向**修正（2026-07-23）。旧版假设的
   「[cmd 2字节大端]」是错的（真机零响应）；真实协议见下，已真机验证：
   0x0101→0x0102 拿到 固件/电量/型号；0x0703 按钮双击真机 27/27 解析成功。

传输层：Nordic UART Service (NUS)
    Service 6e400001-…  Notify(上行) 6e400003-…  Write(下行) 6e400002-…

帧格式（全大端）：
    off 0    uint8   magic = 0x3F
    off 1-2  uint16  version = 0x0004（常量）
    off 3-4  uint16  command
    off 5-8  uint32  payload length
    off 9-10 uint16  CRC16(payload)
    off 11+  payload

命令表（摘自 APK app-service.js）：
    0x0101/0x0102 系统信息(固件/电量/SN/型号)   0x0103/0x0104 系统配置
    0x0401 校时请求(戒指发) / 0x0402 校时应答(手机回)   ← 戒指开机反复发 0x0401
    0x0501.. 录音    0x0601 开六轴/0x0603 停/0x0605 实时六轴帧
    0x0701 gesture recognition / 0x0702 sensor gesture(带手势ID) / 0x0703 按钮双击(P0)
    0x1005.. OTA
手势 ID(0x0702 payload[4])：1=后旋 2=前旋 3=挥手
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

# --- 帧常量 ---
FRAME_MAGIC = 0x3F
FRAME_VERSION = 0x0004
HEADER_LEN = 11

# --- 命令码 ---
CMD_SYS_INFO_REQ = 0x0101
CMD_SYS_INFO_RESP = 0x0102
CMD_TIME_SYNC_REQ = 0x0401       # 戒指 -> 手机：求时间
CMD_TIME_SYNC_ACK = 0x0402       # 手机 -> 戒指：给时间
CMD_REPORT_START = 0x0601
CMD_REPORT_STOP = 0x0603
CMD_IMU_BATCH = 0x0605
CMD_TOUCH_DOUBLE_TAP = 0x0701    # gesture recognition result
CMD_MOTION_GESTURE = 0x0702      # sensor gesture result（带手势 ID）
CMD_BUTTON_DOUBLE_PRESS = 0x0703  # key double press result —— P0 确认信号

# ---- 录音相关（来自 APK 命令表，尚未真机验证；ring_audio_probe.py 用来实测）----
# 注意 0x0503 是「开始**提取**」已有录音，不是「开始录音」。
# 录音由戒指端某个物理动作触发，具体是什么动作文档没写，需要实测。
CMD_AUDIO_LIST = 0x0501          # 录音列表
CMD_AUDIO_LIST_RESP = 0x0502      # 录音列表响应
CMD_AUDIO_EXTRACT_START = 0x0503  # 开始提取某条录音
CMD_AUDIO_METADATA = 0x0504       # 录音元数据
CMD_AUDIO_DATA = 0x0505           # 录音数据帧
CMD_AUDIO_NEXT = 0x0506           # 请求下一段录音数据
CMD_AUDIO_EXTRACT_FINISH = 0x0507 # 结束提取
CMD_AUDIO_TRANSFER_DONE = 0x0508  # 传输完成
CMD_AUDIO_QUICK_PLAY = 0x0509     # 快速播放
CMD_AUDIO_CREATED = 0x050A        # 新录音完成通知

GESTURE_NAMES = {0: "idle", 1: "rotate_back", 2: "rotate_front", 3: "wave"}


class FrameError(ValueError):
    pass


def crc16(data: bytes, seed: int = 0xFFFF) -> int:
    """CRC16（CCITT 变体），与 APK 的 ue() 逐字节实现一致。空 payload → 0xFFFF。"""
    n = seed & 0xFFFF
    for b in data:
        n = 0xFFFF & ((n >> 8) | (n << 8))
        n ^= b
        n ^= (0xFF & n) >> 4
        n ^= (n << 12) & 0xFFFF
        n ^= ((0xFF & n) << 5) & 0xFFFF
    return n & 0xFFFF


@dataclass
class ZiloFrame:
    cmd: int
    body: bytes
    version: int = FRAME_VERSION
    crc_valid: bool = True


def parse_frame(data: bytes) -> ZiloFrame:
    """真实帧解析：magic + ver + cmd + len + crc + body。CRC 不符也返回(crc_valid=False)。"""
    if len(data) < HEADER_LEN:
        raise FrameError(f"帧过短(<{HEADER_LEN}): {data.hex()}")
    if data[0] != FRAME_MAGIC:
        raise FrameError(f"magic 非 0x3F: 0x{data[0]:02X}")
    version, cmd, body_len, crc = struct.unpack(">HHIH", data[1:11])
    body = bytes(data[HEADER_LEN:HEADER_LEN + body_len] if body_len else data[HEADER_LEN:])
    return ZiloFrame(cmd=cmd, body=body, version=version, crc_valid=(crc16(body) == crc))


def build_frame(cmd: int, body: bytes = b"") -> bytes:
    """构造下行帧（含 CRC）：build_frame(0x0601) -> 完整 bytes。"""
    body = body or b""
    return struct.pack(">BHHIH", FRAME_MAGIC, FRAME_VERSION, cmd, len(body), crc16(body)) + body


def build_time_sync_ack(unix_seconds: int) -> bytes:
    """回应戒指 0x0401 校时请求：payload = uint32 大端 Unix 秒。不回它会一直重发。"""
    return build_frame(CMD_TIME_SYNC_ACK, struct.pack(">I", int(unix_seconds) & 0xFFFFFFFF))


def build_audio_extract_start(file_index: int) -> bytes:
    """请求提取已有录音：reserved:u16 + fileIndex:u32。"""
    return build_frame(CMD_AUDIO_EXTRACT_START,
                       struct.pack(">HI", 0, int(file_index) & 0xFFFFFFFF))


def build_audio_next(file_index: int, offset: int) -> bytes:
    """请求录音下一帧：reserved:u16 + fileIndex:u32 + offset:u32 + reserved:u16。"""
    return build_frame(CMD_AUDIO_NEXT, struct.pack(">HIIH", 0,
                                                    int(file_index) & 0xFFFFFFFF,
                                                    int(offset) & 0xFFFFFFFF, 0))


def build_audio_extract_finish(file_index: int) -> bytes:
    """结束一条录音提取：reserved:u16 + fileIndex:u32。"""
    return build_frame(CMD_AUDIO_EXTRACT_FINISH,
                       struct.pack(">HI", 0, int(file_index) & 0xFFFFFFFF))


def parse_audio_list_body(body: bytes) -> dict:
    """0502：error:u16 + fileCount:u32。"""
    error = struct.unpack(">H", body[:2])[0] if len(body) >= 2 else None
    count = struct.unpack(">I", body[2:6])[0] if len(body) >= 6 else 0
    return {"errorCode": error, "fileCount": count}


def parse_audio_metadata_body(body: bytes) -> dict:
    """0504：error:u16 + fileIndex:u32 + recordTime:u32 + dataSize:u32。"""
    return {
        "errorCode": struct.unpack(">H", body[:2])[0] if len(body) >= 2 else None,
        "fileIndex": struct.unpack(">I", body[2:6])[0] if len(body) >= 6 else 0,
        "recordTime": struct.unpack(">I", body[6:10])[0] if len(body) >= 10 else 0,
        "dataSize": struct.unpack(">I", body[10:14])[0] if len(body) >= 14 else 0,
    }


def parse_audio_data_body(body: bytes) -> dict:
    """0505：头部 15 字节后是数据；兼容 APK 的 frameSize/isEnd 字段。"""
    result = {
        "errorCode": struct.unpack(">H", body[:2])[0] if len(body) >= 2 else None,
        "fileIndex": struct.unpack(">I", body[2:6])[0] if len(body) >= 6 else 0,
        "offset": struct.unpack(">I", body[6:10])[0] if len(body) >= 10 else 0,
        "frameSize": struct.unpack(">I", body[10:14])[0] if len(body) >= 14 else 0,
        "isEnd": len(body) >= 15 and body[14] == 1,
    }
    raw = body[15:] if len(body) > 15 else b""
    # APK 兼容三种固件封装：裸数据、头部 u16 长度、尾部 u16 长度。
    data = raw
    size = result["frameSize"]
    if size and len(raw) >= size + 2:
        trailer = raw[size:size + 2]
        if int.from_bytes(trailer, "little") == size or int.from_bytes(trailer, "big") == size:
            data = raw[:size]
        elif int.from_bytes(raw[:2], "little") == size or int.from_bytes(raw[:2], "big") == size:
            data = raw[2:2 + size]
    if size and len(data) >= size:
        data = data[:size]
    result["data"] = bytes(data)
    return result


def hex_to_bytes(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").replace("-", "").replace("0x", ""))


@dataclass
class ParsedIMU:
    seq_start: int
    seq_end: int
    uptime_ms: int
    accel: tuple[int, int, int]
    gyro: tuple[int, int, int]


def parse_imu_body(body: bytes) -> Optional[ParsedIMU]:
    """0x0605 实时六轴帧解析（真机/APK 布局）：
        err(u16) seqStart(u32) frameCount(u16) frameSize(u16)
        然后 frameCount 个 frameSize 字节帧：ts(u32) accel×3(i16) gyro×3(i16)。
    返回代表性首采样（accel/gyro 单条），与上层接口一致。长度不符返回 None。"""
    if len(body) < 10:
        return None
    try:
        err, seq_start, frame_count, frame_size = struct.unpack(">HIHH", body[:10])
    except struct.error:
        return None
    if err != 0 or frame_size < 16 or len(body) < 10 + frame_size:
        return None
    ts, ax, ay, az, gx, gy, gz = struct.unpack(">Ihhhhhh", body[10:26])
    return ParsedIMU(
        seq_start=seq_start,
        seq_end=seq_start + max(frame_count - 1, 0),
        uptime_ms=ts,
        accel=(ax, ay, az),
        gyro=(gx, gy, gz),
    )


def parse_sys_info(body: bytes) -> dict:
    """0x0102 系统信息 -> dict（固件/电量/充电/SN/CPU/型号）。用于"提取资料"展示。"""
    def rd_str(off: int) -> tuple[str, int]:
        if off + 2 > len(body):
            return "", off
        ln = struct.unpack(">H", body[off:off + 2])[0]
        start, end = off + 2, min(len(body), off + 2 + ln)
        return body[start:end].decode("utf-8", "replace"), end

    if len(body) < 2:
        return {"errorCode": None}
    out: dict = {"errorCode": struct.unpack(">H", body[:2])[0]}
    a = 2
    out["firmwareVersion"], a = rd_str(a)
    out["systemTime"] = struct.unpack(">I", body[a:a + 4])[0] if a + 4 <= len(body) else 0
    a += 4
    out["audioStorageTotal"] = struct.unpack(">I", body[a:a + 4])[0] if a + 4 <= len(body) else 0
    a += 4
    out["audioStorageAvailable"] = struct.unpack(">I", body[a:a + 4])[0] if a + 4 <= len(body) else 0
    a += 4
    out["batteryPercent"] = struct.unpack(">H", body[a:a + 2])[0] if a + 2 <= len(body) else 0
    a += 2
    out["batteryCharging"] = bool(a < len(body) and body[a] == 1)
    a += 1
    out["sn"], a = rd_str(a)
    out["cpuId"], a = rd_str(a)
    out["model"], a = rd_str(a)
    return out


def classify(frame: ZiloFrame) -> str:
    """帧 -> 语义事件名。未知命令返回 'unknown'（上层忽略，不抛错）。"""
    return {
        CMD_SYS_INFO_RESP: "sys_info",
        CMD_TIME_SYNC_REQ: "time_sync_req",   # 戒指求时间，需回 0x0402
        CMD_IMU_BATCH: "imu_batch",
        CMD_TOUCH_DOUBLE_TAP: "double_tap",
        CMD_MOTION_GESTURE: "motion_gesture",
        CMD_BUTTON_DOUBLE_PRESS: "double_press_confirm",
    }.get(frame.cmd, "unknown")
