"""内存存储层。黑客松够用；未来换 SQLite/Supabase 只改这一个文件。
线程模型：FastAPI 单事件循环内访问，无需加锁。"""
from __future__ import annotations

import time
from collections import deque
from datetime import timedelta
from typing import Optional

from . import config
from .models import (
    CandidatePair, Encounter, IMUBatch, Sighting, SessionState, UserEventProfile, now,
)


class Store:
    def __init__(self) -> None:
        self.profiles: dict[str, UserEventProfile] = {}
        self.states: dict[str, SessionState] = {}
        self.ephemeral_map: dict[str, str] = {}          # ephemeral_id -> user_id
        self.sightings: deque[Sighting] = deque(maxlen=5000)
        self.pairs: dict[str, CandidatePair] = {}
        self.encounters: dict[str, Encounter] = {}
        self.imu_recent: dict[str, deque[IMUBatch]] = {}

        # 两层冷却
        self.user_quiet_until: dict[str, float] = {}     # user_id -> monotonic 截止
        self.pair_tried_at: dict[frozenset, float] = {}  # {a,b} -> monotonic

    # ---- profiles ----
    def upsert_profile(self, p: UserEventProfile) -> None:
        self.profiles[p.user_id] = p
        self.states.setdefault(p.user_id, SessionState.BLUE_OFFLINE)

    def get_profile(self, user_id: str) -> Optional[UserEventProfile]:
        return self.profiles.get(user_id)

    # ---- state ----
    def get_state(self, user_id: str) -> SessionState:
        return self.states.get(user_id, SessionState.BLUE_OFFLINE)

    def set_state(self, user_id: str, s: SessionState) -> None:
        self.states[user_id] = s

    # ---- ephemeral ----
    def register_ephemeral(self, ephemeral_id: str, user_id: str) -> None:
        self.ephemeral_map[ephemeral_id] = user_id

    def resolve_ephemeral(self, ephemeral_id: str) -> Optional[str]:
        return self.ephemeral_map.get(ephemeral_id)

    # ---- sightings ----
    def add_sighting(self, s: Sighting) -> None:
        self.sightings.append(s)

    def recent_sightings(self, observer: str, ephemeral_id: str) -> list[Sighting]:
        cutoff = now() - timedelta(seconds=config.PRESENCE_WINDOW_SECONDS)
        return [s for s in self.sightings
                if s.observer_user_id == observer
                and s.ephemeral_id == ephemeral_id
                and s.seen_at >= cutoff]

    def dwell_seconds(self, observer: str, ephemeral_id: str) -> float:
        """共同停留时长：窗口内首次与最后一次观测的时间跨度。
        擦肩而过 ≈ 0；在旁边站了两分钟 ≈ 120。仅用于排序。"""
        seen = self.recent_sightings(observer, ephemeral_id)
        if len(seen) < 2:
            return 0.0
        return (seen[-1].seen_at - seen[0].seen_at).total_seconds()

    # ---- pairs ----
    def add_pair(self, p: CandidatePair) -> None:
        self.pairs[p.pair_id] = p

    def get_pair(self, pair_id: str) -> Optional[CandidatePair]:
        return self.pairs.get(pair_id)

    def active_pair_for(self, user_id: str) -> Optional[CandidatePair]:
        for p in self.pairs.values():
            if not self.pair_is_live(p):
                continue
            if user_id in (p.user_a, p.user_b):
                return p
        return None

    def pair_is_live(self, p: CandidatePair) -> bool:
        if p.cancelled:
            return False
        if p.candidate_expires_at and now() > p.candidate_expires_at:
            return False
        return True

    # ---- 冷却：按用户 ----
    def user_is_quiet(self, user_id: str) -> bool:
        """该用户处于静默期（刚被提醒过），不应再收到任何新提醒。"""
        until = self.user_quiet_until.get(user_id)
        return until is not None and time.monotonic() < until

    def mark_user_notified(self, user_id: str) -> None:
        self.user_quiet_until[user_id] = (
            time.monotonic() + config.USER_NOTIFY_COOLDOWN_SECONDS
        )

    def clear_user_quiet(self, user_id: str) -> None:
        """确认失败/切蓝后可提前解除静默（可选，默认不调用）。"""
        self.user_quiet_until.pop(user_id, None)

    # ---- 冷却：按人对 ----
    def pair_recently_tried(self, a: str, b: str) -> bool:
        ts = self.pair_tried_at.get(frozenset((a, b)))
        return ts is not None and (time.monotonic() - ts) < config.PAIR_RETRY_COOLDOWN_SECONDS

    def mark_pair_tried(self, a: str, b: str) -> None:
        self.pair_tried_at[frozenset((a, b))] = time.monotonic()

    # ---- encounters ----
    def add_encounter(self, e: Encounter) -> None:
        self.encounters[e.encounter_id] = e

    # ---- imu (memory only) ----
    def add_imu(self, batch: IMUBatch) -> None:
        dq = self.imu_recent.setdefault(batch.user_id, deque())
        dq.append(batch)
        cutoff = now() - timedelta(seconds=config.IMU_MEMORY_SECONDS)
        while dq and dq[0].received_at < cutoff:
            dq.popleft()

    # ---- 数据清理 ----
    def clear_user(self, user_id: str) -> None:
        self.profiles.pop(user_id, None)
        self.states.pop(user_id, None)
        self.imu_recent.pop(user_id, None)
        self.user_quiet_until.pop(user_id, None)
        self.ephemeral_map = {k: v for k, v in self.ephemeral_map.items() if v != user_id}


store = Store()
