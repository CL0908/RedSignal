"""核心数据结构，字段严格对照 PRD v2.1 第10章。
所有硬件事件（真实/Mock）必须使用同一结构（附录A规则4）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def now() -> datetime:
    return datetime.now(timezone.utc)


class Mode(str, Enum):
    LOVE = "love"      # 红
    FRIEND = "friend"  # 绿
    OFF = "off"        # 蓝


class SessionState(str, Enum):
    BLUE_OFFLINE = "BLUE_OFFLINE"
    DISCOVERABLE = "DISCOVERABLE"
    CANDIDATE_NEARBY = "CANDIDATE_NEARBY"
    NOTIFIED = "NOTIFIED"
    SELF_CONFIRMED = "SELF_CONFIRMED"
    ENCOUNTER_CONFIRMED = "ENCOUNTER_CONFIRMED"
    CONNECTED = "CONNECTED"
    CONTENT_READY = "CONTENT_READY"
    CANCELLED = "CANCELLED"


class ButtonEventType(str, Enum):
    DOUBLE_PRESS_CONFIRM = "double_press_confirm"  # 0x0703 实体按钮双击 = P0 确认
    DOUBLE_TAP = "double_tap"                      # 0x0701 触摸双击（暂不用于确认）


# 分享包字段分级（share_bundle.py 强制执行）
REQUIRED_FIELDS = ["nickname", "shared_interests"]
OPTIONAL_FIELDS = ["avatar", "bio", "wechat", "xiaohongshu", "instagram", "github", "team_need"]
FORBIDDEN_FIELDS = ["phone", "real_name", "precise_location", "health", "raw_audio"]


@dataclass
class UserEventProfile:
    user_id: str
    event_id: str
    mode: Mode
    social_goal: str
    interest_tags: list[str]
    communication_style: str
    share_bundle: dict[str, str]          # 字段名 -> 值（仅用户预先授权的字段）
    nickname: str
    expires_at: Optional[datetime] = None
    blocked_users: set[str] = field(default_factory=set)


@dataclass
class RollingPresence:
    """BLE 广播内容——仅此四个字段，不得扩充。"""
    ephemeral_id: str
    event_id: str
    mode_code: str        # "R" | "G" | "B"
    timestamp_bucket: int


@dataclass
class Sighting:
    """一次扫描上报：某用户（或中央基站代表的用户视角）看到了某个 ephemeral_id。"""
    observer_user_id: str
    ephemeral_id: str
    rssi: int
    seen_at: datetime = field(default_factory=now)


@dataclass
class CandidatePair:
    pair_id: str
    user_a: str
    user_b: str
    mode: Mode
    match_score: int
    proximity_band: str   # very_near | near | far
    created_at: datetime = field(default_factory=now)
    candidate_expires_at: Optional[datetime] = None
    cancelled: bool = False


@dataclass
class RingButtonEvent:
    user_id: str
    pair_id: str
    event_type: ButtonEventType
    detected_at: datetime = field(default_factory=now)
    device_id: str = "mock"


@dataclass
class Encounter:
    encounter_id: str
    pair_id: str
    confirmed_by: list[str]
    confirmation_method: str              # dual_ring_button | app_double_confirm
    shared_fields: dict[str, dict[str, object]]  # 接收方user_id -> 其可见的对方字段
    optional_gesture: Optional[str] = None
    created_at: datetime = field(default_factory=now)
    agent_content: Optional[dict] = None


@dataclass
class IMUBatch:
    """0x0605 批量帧。只留内存，不入库。"""
    user_id: str
    seq_start: int
    seq_end: int
    uptime_ms: int
    accel: tuple[int, int, int]
    gyro: tuple[int, int, int]
    received_at: datetime = field(default_factory=now)
