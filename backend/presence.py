"""BLE 候选持续性判断与推送触发（PRD 5.2 / 8.3）。
扫描由客户端（手机或 T5AI 中央基站）完成并上报；本模块只负责：
  1. 记录观测（含 RSSI 与时间戳，供停留时长计算）
  2. 每次上报后重新计算当下最优候选——不维护静态队列，
     因为密集场景（舞池）里人流变化快，队列几秒就过期了
"""
from __future__ import annotations

import logging
from typing import Optional

from .models import CandidatePair, Sighting, SessionState
from .store import store
from . import matching
from .state_machine import transition

log = logging.getLogger("redsignal.presence")
S = SessionState


def report_sighting(observer_user_id: str, ephemeral_id: str,
                    rssi: int) -> tuple[Optional[CandidatePair], str]:
    """上报一次扫描结果。返回 (新建的Pair|None, 原因)。"""
    store.add_sighting(Sighting(observer_user_id, ephemeral_id, rssi))

    if store.resolve_ephemeral(ephemeral_id) is None:
        return None, "unknown_ephemeral"

    cand, reason = matching.best_pair_for(observer_user_id)
    if cand is None:
        return None, reason

    pair = matching.create_pair(observer_user_id, cand)
    log.info("pair %s: %s <-> %s compat=%d rank=%.1f dwell=%.0fs",
             pair.pair_id, pair.user_a, pair.user_b,
             cand.compat_score, cand.rank_score, cand.dwell_seconds)

    for uid in (pair.user_a, pair.user_b):
        if store.get_state(uid) == S.DISCOVERABLE:
            transition(uid, S.CANDIDATE_NEARBY)
        transition(uid, S.NOTIFIED)
    return pair, "matched"
