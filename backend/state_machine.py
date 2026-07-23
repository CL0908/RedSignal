"""会话状态机（PRD 9.4）。
规则：切蓝在任何状态立即生效（附录A规则7）；非法转移抛 InvalidTransition。"""
from __future__ import annotations

from .models import Mode, SessionState
from .store import store

S = SessionState

ALLOWED: dict[SessionState, set[SessionState]] = {
    S.BLUE_OFFLINE:        {S.DISCOVERABLE},
    S.DISCOVERABLE:        {S.CANDIDATE_NEARBY},
    S.CANDIDATE_NEARBY:    {S.NOTIFIED, S.DISCOVERABLE},
    S.NOTIFIED:            {S.SELF_CONFIRMED, S.DISCOVERABLE},
    S.SELF_CONFIRMED:      {S.ENCOUNTER_CONFIRMED, S.DISCOVERABLE},
    S.ENCOUNTER_CONFIRMED: {S.CONNECTED},
    S.CONNECTED:           {S.CONTENT_READY, S.DISCOVERABLE},
    S.CONTENT_READY:       {S.DISCOVERABLE},
    S.CANCELLED:           {S.DISCOVERABLE},
}


class InvalidTransition(Exception):
    pass


def transition(user_id: str, target: SessionState) -> SessionState:
    """普通转移。切蓝请用 go_blue()，不走此函数。"""
    current = store.get_state(user_id)
    if target == current:
        return current
    if target not in ALLOWED.get(current, set()):
        raise InvalidTransition(f"{user_id}: {current.value} -> {target.value} 不允许")
    store.set_state(user_id, target)
    return target


def go_blue(user_id: str) -> SessionState:
    """蓝色最高优先级：任何状态立即 CANCELLED，取消候选与未完成确认。"""
    store.set_state(user_id, S.CANCELLED)
    pair = store.active_pair_for(user_id)
    if pair is not None:
        pair.cancelled = True
    profile = store.get_profile(user_id)
    if profile:
        profile.mode = Mode.OFF
    store.set_state(user_id, S.BLUE_OFFLINE)
    return S.BLUE_OFFLINE


def set_mode(user_id: str, mode: Mode) -> SessionState:
    """模式切换入口：红/绿 -> DISCOVERABLE；蓝 -> go_blue。"""
    if mode == Mode.OFF:
        return go_blue(user_id)
    profile = store.get_profile(user_id)
    if profile:
        profile.mode = mode
    current = store.get_state(user_id)
    if current in (S.BLUE_OFFLINE, S.CANCELLED, S.CONTENT_READY, S.CONNECTED):
        store.set_state(user_id, S.DISCOVERABLE)
    elif current == S.DISCOVERABLE:
        pass
    else:
        # 已在流程中改红/绿模式：回到 DISCOVERABLE 重新发现
        store.set_state(user_id, S.DISCOVERABLE)
    return store.get_state(user_id)
