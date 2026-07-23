"""双按钮确认器（PRD 5.4 / 6.1-E）——P0 最关键模块。
规则：
- 双方在确认窗口(默认30s)内各自完成一次"按钮双击"才建立 Encounter；
- 单方确认静默过期，绝不显示"对方拒绝"；
- 候选过期/取消后按钮事件不建立连接；
- 防抖：同一用户同一 pair 在 BUTTON_DEBOUNCE_SECONDS 内重复事件只记一次；
- App 双确认与实体按钮走完全相同的逻辑，仅 confirmation_method 不同。"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Optional

from . import config
from .models import (
    ButtonEventType, Encounter, RingButtonEvent, SessionState, now,
)
from .store import store
from .state_machine import transition
from . import share_bundle

S = SessionState

# pair_id -> {user_id: confirmed_at}
_confirmations: dict[str, dict[str, object]] = {}
# pair_id -> 窗口截止时间
_window_deadline: dict[str, object] = {}
# (pair_id, user_id) -> 上次按键时间（防抖）
_last_press: dict[tuple[str, str], object] = {}


class ConfirmResult:
    def __init__(self, status: str, encounter: Optional[Encounter] = None):
        self.status = status          # accepted | duplicate | pair_dead | window_closed |
                                      # wrong_user | encounter_created | ignored_event_type
        self.encounter = encounter


def reset() -> None:
    """测试用。"""
    _confirmations.clear()
    _window_deadline.clear()
    _last_press.clear()


def handle_button_event(ev: RingButtonEvent,
                        confirmation_method: str = "dual_ring_button") -> ConfirmResult:
    # 只有实体按钮双击才是确认信号
    if ev.event_type != ButtonEventType.DOUBLE_PRESS_CONFIRM:
        return ConfirmResult("ignored_event_type")

    pair = store.get_pair(ev.pair_id)
    if pair is None or not store.pair_is_live(pair):
        return ConfirmResult("pair_dead")
    if ev.user_id not in (pair.user_a, pair.user_b):
        return ConfirmResult("wrong_user")

    # 防抖
    key = (ev.pair_id, ev.user_id)
    last = _last_press.get(key)
    if last is not None and (ev.detected_at - last).total_seconds() < config.BUTTON_DEBOUNCE_SECONDS:
        return ConfirmResult("duplicate")
    _last_press[key] = ev.detected_at

    # 窗口：首个确认启动窗口
    deadline = _window_deadline.get(ev.pair_id)
    if deadline is None:
        deadline = ev.detected_at + timedelta(seconds=config.CONFIRM_WINDOW_SECONDS)
        _window_deadline[ev.pair_id] = deadline
    if ev.detected_at > deadline:
        expire_pair(ev.pair_id)
        return ConfirmResult("window_closed")

    confirmed = _confirmations.setdefault(ev.pair_id, {})
    confirmed[ev.user_id] = ev.detected_at
    transition(ev.user_id, S.SELF_CONFIRMED)

    if len(confirmed) < 2:
        return ConfirmResult("accepted")

    # 双确认达成
    encounter = _create_encounter(pair.pair_id, list(confirmed.keys()), confirmation_method)
    return ConfirmResult("encounter_created", encounter)


def _create_encounter(pair_id: str, confirmed_by: list[str],
                      confirmation_method: str) -> Encounter:
    pair = store.get_pair(pair_id)
    assert pair is not None
    shared = share_bundle.exchange(pair.user_a, pair.user_b)
    encounter = Encounter(
        encounter_id=f"enc_{uuid.uuid4().hex[:8]}",
        pair_id=pair_id,
        confirmed_by=confirmed_by,
        confirmation_method=confirmation_method,
        shared_fields=shared,
    )
    store.add_encounter(encounter)
    pair.cancelled = True  # 候选完成使命
    for uid in confirmed_by:
        transition(uid, S.ENCOUNTER_CONFIRMED)
        transition(uid, S.CONNECTED)
    _cleanup(pair_id)
    return encounter


def check_window_expiry(pair_id: str) -> bool:
    """定时/懒惰检查：窗口超时且未双确认 -> 静默过期。返回是否发生过期。"""
    deadline = _window_deadline.get(pair_id)
    if deadline is None:
        return False
    confirmed = _confirmations.get(pair_id, {})
    if now() > deadline and len(confirmed) < 2:
        expire_pair(pair_id)
        return True
    return False


def expire_pair(pair_id: str) -> None:
    """单方确认过期：回 DISCOVERABLE，UI 只显示"未建立连接"。"""
    pair = store.get_pair(pair_id)
    if pair:
        pair.cancelled = True
        for uid in (pair.user_a, pair.user_b):
            st = store.get_state(uid)
            if st in (S.NOTIFIED, S.SELF_CONFIRMED, S.CANDIDATE_NEARBY):
                store.set_state(uid, S.DISCOVERABLE)
    _cleanup(pair_id)


def _cleanup(pair_id: str) -> None:
    _confirmations.pop(pair_id, None)
    _window_deadline.pop(pair_id, None)
    for key in [k for k in _last_press if k[0] == pair_id]:
        _last_press.pop(key, None)
