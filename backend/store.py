"""内存存储层 + Supabase 写回。
线程模型：FastAPI 单事件循环内访问，无需加锁。

内存仍是唯一事实来源：所有读路径零延迟、可离线演示，语义与接入 DB 前完全一致。
写入点额外把值得留档的四类数据异步镜像到 Postgres（见 persistence.py）：
profiles / candidate_pairs / ring_button_events / encounters。
sightings 与 IMU 是热路径，不落库。
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config, tags
from .models import (
    CandidatePair, Encounter, IMUBatch, RingButtonEvent, Sighting, SessionState,
    UserEventProfile, now,
)
from .persistence import persistence


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _is_uuid(s: str) -> bool:
    """真实用户 id = Supabase auth uid（uuid）；预置演示用户是字面量。"""
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


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
        self._persist_profile(p)

    def get_profile(self, user_id: str) -> Optional[UserEventProfile]:
        return self.profiles.get(user_id)

    def _persist_profile(self, p: UserEventProfile) -> None:
        """整行 upsert。normalized_tags 在写入时算好，让 SQL 侧也能直接算重合度。"""
        persistence.write("events", {"event_id": p.event_id, "name": p.event_id},
                          on_conflict="event_id")
        persistence.write("user_event_profiles", {
            "user_id": p.user_id,
            # 真实用户的 user_id 就是 Supabase auth uid，挂上外键让注销能级联删档案；
            # 预置演示用户（'u_demo_a'/'d01'）不是 uuid，留 null
            "auth_user_id": p.user_id if _is_uuid(p.user_id) else None,
            "event_id": p.event_id,
            "nickname": p.nickname,
            "mode": p.mode.value,
            "social_goal": p.social_goal,
            "communication_style": p.communication_style,
            "interest_tags": p.interest_tags,
            "normalized_tags": tags.normalize_tags(p.interest_tags),
            "share_bundle": p.share_bundle,
            "state": self.get_state(p.user_id).value,
            "expires_at": _iso(p.expires_at),
            "updated_at": _iso(now()),
        }, on_conflict="user_id,event_id")
        for blocked in p.blocked_users:
            persistence.write("blocks", {
                "event_id": p.event_id, "user_id": p.user_id,
                "blocked_user_id": blocked,
            }, on_conflict="event_id,user_id,blocked_user_id")

    # ---- state ----
    def get_state(self, user_id: str) -> SessionState:
        return self.states.get(user_id, SessionState.BLUE_OFFLINE)

    def set_state(self, user_id: str, s: SessionState) -> None:
        prev = self.states.get(user_id)
        self.states[user_id] = s
        if prev == s:
            return
        p = self.profiles.get(user_id)
        if p is None:
            return
        persistence.patch("user_event_profiles",
                          f"user_id=eq.{user_id}&event_id=eq.{p.event_id}",
                          {"state": s.value, "mode": p.mode.value,
                           "updated_at": _iso(now())})
        persistence.write("state_transitions", {
            "user_id": user_id, "event_id": p.event_id,
            "from_state": prev.value if prev else None, "to_state": s.value,
        })

    # ---- ephemeral ----
    def register_ephemeral(self, ephemeral_id: str, user_id: str) -> None:
        self.ephemeral_map[ephemeral_id] = user_id
        p = self.profiles.get(user_id)
        persistence.write("ephemeral_ids", {
            "ephemeral_id": ephemeral_id, "user_id": user_id,
            "event_id": p.event_id if p else config.DEFAULT_EVENT_ID,
        }, on_conflict="ephemeral_id")

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

    # ---- mode ----
    def update_mode(self, user_id: str, mode) -> None:
        """模式变更的唯一入口，保证 DB 与内存一致（state_machine 调用）。"""
        p = self.profiles.get(user_id)
        if p is None:
            return
        p.mode = mode
        persistence.patch("user_event_profiles",
                          f"user_id=eq.{user_id}&event_id=eq.{p.event_id}",
                          {"mode": mode.value, "updated_at": _iso(now())})

    # ---- pairs ----
    def add_pair(self, p: CandidatePair) -> None:
        self.pairs[p.pair_id] = p
        persistence.write("candidate_pairs", {
            "pair_id": p.pair_id,
            "event_id": (self.profiles[p.user_a].event_id
                         if p.user_a in self.profiles else config.DEFAULT_EVENT_ID),
            "user_a": p.user_a,
            "user_b": p.user_b,
            "mode": p.mode.value,
            "match_score": p.match_score,
            "proximity_band": p.proximity_band,
            "created_at": _iso(p.created_at),
            "candidate_expires_at": _iso(p.candidate_expires_at),
            "cancelled": p.cancelled,
        })

    def cancel_pair(self, p: CandidatePair, reason: str) -> None:
        """取消候选的唯一入口（切蓝 / 窗口过期 / 已成 encounter）。"""
        p.cancelled = True
        persistence.patch("candidate_pairs", f"pair_id=eq.{p.pair_id}",
                          {"cancelled": True, "cancel_reason": reason})

    def set_pair_breakdown(self, pair_id: str, breakdown: dict) -> None:
        """打分明细单独写，保留可解释性（matching 算完后调用）。"""
        persistence.patch("candidate_pairs", f"pair_id=eq.{pair_id}",
                          {"score_breakdown": breakdown})

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
        # 内存用 monotonic（不受系统时钟跳变影响）；DB 存绝对时间，重启后仍可恢复冷却
        p = self.profiles.get(user_id)
        if p is not None:
            until = now() + timedelta(seconds=config.USER_NOTIFY_COOLDOWN_SECONDS)
            persistence.patch("user_event_profiles",
                              f"user_id=eq.{user_id}&event_id=eq.{p.event_id}",
                              {"quiet_until": _iso(until)})

    def clear_user_quiet(self, user_id: str) -> None:
        """确认失败/切蓝后可提前解除静默（可选，默认不调用）。"""
        self.user_quiet_until.pop(user_id, None)

    # ---- 冷却：按人对 ----
    def pair_recently_tried(self, a: str, b: str) -> bool:
        ts = self.pair_tried_at.get(frozenset((a, b)))
        return ts is not None and (time.monotonic() - ts) < config.PAIR_RETRY_COOLDOWN_SECONDS

    def mark_pair_tried(self, a: str, b: str) -> None:
        self.pair_tried_at[frozenset((a, b))] = time.monotonic()

    # ---- button events（双方同意的证据链，必须留档） ----
    def record_button_event(self, ev: RingButtonEvent) -> None:
        persistence.write("ring_button_events", {
            "pair_id": ev.pair_id,
            "user_id": ev.user_id,
            "event_type": ev.event_type.value,
            "device_id": ev.device_id,
            "detected_at": _iso(ev.detected_at),
        }, on_conflict="pair_id,user_id,event_type")

    # ---- encounters ----
    def add_encounter(self, e: Encounter) -> None:
        self.encounters[e.encounter_id] = e
        persistence.write("encounters", {
            "encounter_id": e.encounter_id,
            "pair_id": e.pair_id,
            "confirmed_by": e.confirmed_by,
            "confirmation_method": e.confirmation_method,
            "shared_fields": e.shared_fields,
            "optional_gesture": e.optional_gesture,
            "created_at": _iso(e.created_at),
        }, on_conflict="encounter_id")

    def set_agent_content(self, encounter_id: str, content: dict) -> None:
        """Agent 内容异步生成，晚于 encounter 落库，单独 patch。"""
        e = self.encounters.get(encounter_id)
        if e is not None:
            e.agent_content = content
        persistence.patch("encounters", f"encounter_id=eq.{encounter_id}",
                          {"agent_content": content})

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
