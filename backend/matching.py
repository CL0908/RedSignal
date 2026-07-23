"""规则匹配与推荐排序引擎（PRD 第8章）。
确定性代码，LLM 不参与任何判断（附录A规则6）——同样输入必然同样输出，
保证主演示连跑三次结果一致。

两个分数，职责分离：
  compat_score  兼容分 0-100，PRD 加权公式，只用于"够不够格"（阈值 80）
  rank_score    排序分 = compat + 停留奖励，只用于"先推谁"

为什么这么拆：停留时长长不代表更合适，只代表更值得优先打扰；
把它混进兼容分会让"阈值 80"这个承诺失去意义。

稳定性：因为 compat_score 对称（score(A,B) == score(B,A)），
按分数降序贪心选边产生的就是唯一稳定匹配——不存在两个人互相更想要对方
却被拆开的情况。因此不需要跑完整 Gale-Shapley。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from . import config, tags
from .models import CandidatePair, Mode, UserEventProfile, now
from .preference import preference
from .store import store

# 社交目标兼容表（对称）
GOAL_AFFINITY = {
    frozenset(("project_teammate", "project_teammate")): 1.0,
    frozenset(("project_teammate", "industry_chat")): 0.8,
    frozenset(("project_teammate", "hobby_friend")): 0.5,
    frozenset(("hobby_friend", "hobby_friend")): 1.0,
    frozenset(("hobby_friend", "event_buddy")): 0.8,
    frozenset(("hobby_friend", "long_term_friend")): 0.7,
    frozenset(("event_buddy", "event_buddy")): 1.0,
    frozenset(("event_buddy", "long_term_friend")): 0.6,
    frozenset(("industry_chat", "industry_chat")): 1.0,
    frozenset(("industry_chat", "long_term_friend")): 0.6,
    frozenset(("long_term_friend", "long_term_friend")): 1.0,
}
GOAL_DEFAULT = 0.5


@dataclass
class Candidate:
    """一个候选对象及其分数明细（便于调试与解释，不暴露给用户）。"""
    user_id: str
    compat_score: int
    rank_score: float
    dwell_seconds: float
    proximity_band: str
    breakdown: dict[str, float]


# ---------------- 硬性门槛 ----------------
def hard_gates(a: UserEventProfile, b: UserEventProfile) -> Optional[str]:
    """返回 None = 通过；否则返回失败原因（只写日志，不暴露给用户，PRD 6.1-D）。"""
    if a.user_id == b.user_id:
        return "same_user"
    if a.event_id != b.event_id:
        return "different_event"
    if a.mode == Mode.OFF or b.mode == Mode.OFF:
        return "blue_mode"
    if a.mode != b.mode:
        return "mode_mismatch"          # 红绿默认互不匹配（PRD 4.3）
    if b.user_id in a.blocked_users or a.user_id in b.blocked_users:
        return "blocked"
    if store.pair_recently_tried(a.user_id, b.user_id):
        return "pair_cooldown"          # 这一对推过没成，整场不再重复
    return None


# ---------------- 兼容分 ----------------
def jaccard(x: list[str], y: list[str]) -> float:
    sx, sy = set(x), set(y)
    if not sx or not sy:
        return 0.0
    return len(sx & sy) / len(sx | sy)


def score_breakdown(a: UserEventProfile, b: UserEventProfile) -> dict[str, float]:
    """PRD 8.1 绿色评分：40 兴趣 + 25 目标 + 20 沟通 + 15 场景。

    兴趣项两步处理：先同义词归一化（AI Agent = 人工智能代理），
    再算带领域部分分的重合度（ai-agent 与 llm 同属 ai 领域，给部分分）。
    """
    interest = tags.soft_overlap(a.interest_tags, b.interest_tags)
    goal = GOAL_AFFINITY.get(frozenset((a.social_goal, b.social_goal)), GOAL_DEFAULT)
    comms = 1.0 if a.communication_style == b.communication_style else 0.6
    scene = 1.0            # 同一活动即满分；未来可按活动子区域细化
    return {
        "interest": 40 * interest,
        "goal": 25 * goal,
        "comms": 20 * comms,
        "scene": 15 * scene,
    }


def compat_score(a: UserEventProfile, b: UserEventProfile) -> int:
    return round(sum(score_breakdown(a, b).values()))


# ---------------- 排序信号 ----------------
def dwell_bonus(dwell_s: float) -> float:
    """停留时长奖励，线性饱和。擦肩而过 0 分，站够 2 分钟拿满 10 分。"""
    if dwell_s <= 0:
        return 0.0
    ratio = min(1.0, dwell_s / config.DWELL_SATURATE_SECONDS)
    return config.DWELL_BONUS_MAX * ratio


def proximity_band(rssi_values: list[int]) -> str:
    """RSSI 中位数分段——只输出三档，不换算米数（PRD 8.3）。"""
    if not rssi_values:
        return "far"
    med = sorted(rssi_values)[len(rssi_values) // 2]
    if med >= config.RSSI_VERY_NEAR:
        return "very_near"
    if med >= config.RSSI_NEAR:
        return "near"
    return "far"


# ---------------- 候选收集与排序 ----------------
def collect_candidates(observer_id: str) -> tuple[list[Candidate], dict[str, str]]:
    """扫描 observer 视角下所有在场对象，返回 (合格候选按分降序, 淘汰原因表)。"""
    me = store.get_profile(observer_id)
    rejected: dict[str, str] = {}
    if me is None:
        return [], {"_": "profile_missing"}

    out: list[Candidate] = []
    for eph, other_id in store.ephemeral_map.items():
        if other_id == observer_id:
            continue
        other = store.get_profile(other_id)
        if other is None:
            continue

        gate = hard_gates(me, other)
        if gate:
            rejected[other_id] = gate
            continue

        seen = store.recent_sightings(observer_id, eph)
        if len(seen) < config.PRESENCE_MIN_SIGHTINGS:
            rejected[other_id] = f"need_more_sightings({len(seen)}/{config.PRESENCE_MIN_SIGHTINGS})"
            continue

        bd = score_breakdown(me, other)
        cs = round(sum(bd.values()))
        if cs < config.MATCH_SCORE_THRESHOLD:
            rejected[other_id] = f"score_below_threshold({cs})"
            continue

        dwell = store.dwell_seconds(observer_id, eph)
        # 学到的偏好：observer 视角的排序加分（不改兼容分/阈值，保持准入对称）
        pref = preference.preference_bonus(observer_id, other.interest_tags)
        out.append(Candidate(
            user_id=other_id,
            compat_score=cs,
            rank_score=cs + dwell_bonus(dwell) + pref,
            dwell_seconds=dwell,
            proximity_band=proximity_band([s.rssi for s in seen]),
            breakdown={**bd, "pref_bonus": pref},
        ))

    # 降序排序；分数相同时按 user_id 排，保证结果可复现
    out.sort(key=lambda c: (-c.rank_score, c.user_id))
    return out, rejected


def eligible_to_notify(user_id: str) -> bool:
    """该用户此刻能否收到新提醒：不在静默期、不在进行中的配对里。"""
    from .models import SessionState as S
    if store.user_is_quiet(user_id):
        return False
    if store.active_pair_for(user_id) is not None:
        return False
    return store.get_state(user_id) in (S.DISCOVERABLE, S.CANDIDATE_NEARBY)


def best_pair_for(observer_id: str) -> tuple[Optional[Candidate], str]:
    """重算当下最优候选（不是队列里的第一个——密集场景下队列会立刻过期）。
    双方都必须能接收提醒。"""
    if not eligible_to_notify(observer_id):
        return None, "observer_not_eligible"

    ranked, rejected = collect_candidates(observer_id)
    if not ranked:
        return None, (next(iter(rejected.values())) if rejected else "no_candidate")

    for cand in ranked:
        if eligible_to_notify(cand.user_id):
            return cand, "ok"
    return None, "all_candidates_busy"


def create_pair(observer_id: str, cand: Candidate) -> CandidatePair:
    """锁定一对候选：建 Pair + 双方进入静默期 + 记录该对已尝试。"""
    me = store.get_profile(observer_id)
    pair = CandidatePair(
        pair_id=f"pair_{uuid.uuid4().hex[:8]}",
        user_a=observer_id,
        user_b=cand.user_id,
        mode=me.mode,
        match_score=cand.compat_score,
        proximity_band=cand.proximity_band,
        candidate_expires_at=now() + timedelta(seconds=config.CANDIDATE_TTL_SECONDS),
    )
    store.add_pair(pair)
    store.mark_user_notified(observer_id)
    store.mark_user_notified(cand.user_id)
    store.mark_pair_tried(observer_id, cand.user_id)
    return pair


def try_create_pair(observer_id: str, target_id: str,
                    rssi_values: list[int]) -> tuple[Optional[CandidatePair], str]:
    """兼容旧接口（指定目标）。新代码请用 best_pair_for + create_pair。"""
    ranked, rejected = collect_candidates(observer_id)
    if not eligible_to_notify(observer_id):
        return None, "observer_not_eligible"
    for c in ranked:
        if c.user_id == target_id:
            if not eligible_to_notify(target_id):
                return None, "target_not_eligible"
            return create_pair(observer_id, c), "ok"
    return None, rejected.get(target_id, "no_candidate")


# ---------------- 全局稳定配对（多人同时在场时用）----------------
def stable_round(event_id: str) -> list[CandidatePair]:
    """一轮全局撮合：把当前在场所有人一次性配对。

    做法：枚举所有合格人对，按 rank_score 降序贪心锁定。
    因为分数对称，这产生的就是唯一稳定匹配——不会出现
    A、B 互为最优却被分别配给了别人的情况。

    舞池等密集场景用这个，比逐个 observer 触发更公平。
    """
    users = [uid for uid, p in store.profiles.items()
             if p.event_id == event_id and eligible_to_notify(uid)]

    edges: list[tuple[float, int, str, str]] = []
    for i, ua in enumerate(users):
        ranked, _ = collect_candidates(ua)
        for c in ranked:
            if c.user_id <= ua:          # 每对只算一次
                continue
            edges.append((c.rank_score, c.compat_score, ua, c.user_id))

    # 降序；同分时按 user_id 保证可复现
    edges.sort(key=lambda e: (-e[0], e[2], e[3]))

    locked: set[str] = set()
    created: list[CandidatePair] = []
    for rank_s, compat_s, ua, ub in edges:
        if ua in locked or ub in locked:
            continue
        cand = Candidate(user_id=ub, compat_score=compat_s, rank_score=rank_s,
                         dwell_seconds=0.0, proximity_band="near", breakdown={})
        created.append(create_pair(ua, cand))
        locked.add(ua)
        locked.add(ub)
    return created
