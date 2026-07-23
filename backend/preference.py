"""偏好学习层 —— 从「聊天行为」学到「你还喜欢哪类人」，只影响排序，不影响准入。

设计红线（与 matching.py 一致）：
  - compat_score（兼容分，阈值 80）保持对称、确定、不含学习——够不够格的判断不变。
  - 学习到的偏好只作为 **rank_score 的加分**（像 dwell_bonus 一样），
    是 observer 视角、非对称的「先推谁」信号，不改变「够不够格」。
  - 学习信号来自**聊天行为**（频率/回复速度/天数 + Agent 评的融洽度），
    **不含任何生理数据**（心率/血氧只做个人展示，绝不进匹配）。

工作原理：
  每个用户有一张 tag -> 权重 表（初始 1.0）。一次聊天结束后：
    engagement = f(消息数, 回复速度, 活跃天数, rapport)   ∈ [0,1]
    对聊得好的对象，其每个 tag 的权重 += lr * (engagement - 0.5)
  下次匹配时，preference_bonus(observer, 对方tags) 用这张权重表打分，
  权重高的 tag 越多 → 排序越靠前 → 「你可能也喜欢这类人」自然涌现。

纯 Python、可测、可复现。Agent 只负责评 rapport（一个 0-1 的软信号），其余全确定。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import config, tags

# 排序加分上限（与 DWELL_BONUS_MAX 同量级，保证学习信号可感知但不淹没兼容分）
PREF_BONUS_MAX = getattr(config, "PREF_BONUS_MAX", 8.0)
PREF_LEARNING_RATE = getattr(config, "PREF_LEARNING_RATE", 0.35)
PREF_WEIGHT_MIN = 0.2
PREF_WEIGHT_MAX = 3.0


# ---------------- 聊天行为 -> engagement（确定性部分）----------------
@dataclass
class EngagementMetrics:
    message_count: int = 0
    reply_speed_score: float = 0.0   # 0-1，回复越快越高
    active_days: int = 0
    rapport: float = 0.5             # 0-1，Agent 评的融洽度（缺省中性）
    engagement: float = 0.0          # 0-1，四项综合

    def to_dict(self) -> dict:
        return {
            "message_count": self.message_count,
            "reply_speed_score": round(self.reply_speed_score, 3),
            "active_days": self.active_days,
            "rapport": round(self.rapport, 3),
            "engagement": round(self.engagement, 3),
        }


def compute_engagement(messages: list[dict], rapport: float = 0.5) -> EngagementMetrics:
    """从聊天记录（确定性）+ rapport（Agent 软信号）算综合 engagement ∈ [0,1]。

    messages: [{"sender": user_id, "ts": epoch_seconds, "text": str}, ...] 按时间升序。
    四个维度各占权重，全部有界，缺数据时安全退化。
    """
    n = len(messages)
    if n == 0:
        return EngagementMetrics(rapport=rapport, engagement=0.0)

    # 1) 消息数：对数饱和，20 条以上基本拿满
    msg_score = min(1.0, math.log1p(n) / math.log1p(20))

    # 2) 回复速度：相邻「换人」消息的间隔中位数，越短越高（5 分钟内算好）
    gaps: list[float] = []
    for i in range(1, n):
        prev, cur = messages[i - 1], messages[i]
        if prev.get("sender") != cur.get("sender"):
            dt = float(cur.get("ts", 0)) - float(prev.get("ts", 0))
            if dt >= 0:
                gaps.append(dt)
    if gaps:
        med_gap = sorted(gaps)[len(gaps) // 2]
        reply_speed = max(0.0, 1.0 - min(1.0, med_gap / 300.0))  # 300s 饱和
    else:
        reply_speed = 0.0

    # 3) 活跃天数：不同「日期桶」个数，3 天以上拿满
    day_buckets = {int(float(m.get("ts", 0)) // 86400) for m in messages}
    active_days = len(day_buckets)
    days_score = min(1.0, active_days / 3.0)

    # 4) rapport：Agent 评的融洽度（已在 0-1）
    rapport = max(0.0, min(1.0, rapport))

    engagement = (
        0.30 * msg_score
        + 0.25 * reply_speed
        + 0.20 * days_score
        + 0.25 * rapport
    )
    return EngagementMetrics(
        message_count=n,
        reply_speed_score=reply_speed,
        active_days=active_days,
        rapport=rapport,
        engagement=max(0.0, min(1.0, engagement)),
    )


# ---------------- 偏好模型（每个用户一张 tag 权重表）----------------
@dataclass
class PreferenceModel:
    """内存态偏好表。可换成 store 持久化，接口不变。"""
    # user_id -> {tag -> weight}
    _weights: dict[str, dict[str, float]] = field(default_factory=dict)

    def weights_for(self, user_id: str) -> dict[str, float]:
        return self._weights.setdefault(user_id, {})

    def update_from_chat(self, observer_id: str, partner_tags: list[str],
                         engagement: float) -> dict[str, float]:
        """一次聊天后更新 observer 对「这类 tag」的偏好。

        engagement>0.5 的对象把其 tag 权重上调，<0.5 下调。归一化标签后更新。
        """
        w = self.weights_for(observer_id)
        delta = PREF_LEARNING_RATE * (engagement - 0.5) * 2.0  # 映射到 [-lr, +lr]
        for t in tags.normalize_tags(partner_tags):
            cur = w.get(t, 1.0)
            w[t] = max(PREF_WEIGHT_MIN, min(PREF_WEIGHT_MAX, cur + delta))
        return dict(w)

    def preference_bonus(self, observer_id: str, partner_tags: list[str]) -> float:
        """排序加分 ∈ [0, PREF_BONUS_MAX]：对方 tag 在 observer 偏好表里越受青睐越高。

        用「超过 1.0 的权重」求平均后缩放——没学过任何东西时返回 0（不影响排序）。
        """
        w = self._weights.get(observer_id)
        if not w:
            return 0.0
        norm = tags.normalize_tags(partner_tags)
        if not norm:
            return 0.0
        # 每个 tag 的「偏好增量」= weight-1（可正可负），取正向平均
        lifts = [w.get(t, 1.0) - 1.0 for t in norm]
        avg_lift = sum(lifts) / len(lifts)
        # avg_lift ∈ [PREF_WEIGHT_MIN-1, PREF_WEIGHT_MAX-1] = [-0.8, +2.0]
        # 只奖励正向，负向不惩罚排序（避免把学过的坏对象推到最后一名反而显眼）
        pos = max(0.0, avg_lift)
        scaled = min(1.0, pos / (PREF_WEIGHT_MAX - 1.0))  # 归一到 0-1
        return PREF_BONUS_MAX * scaled

    def top_tags(self, observer_id: str, k: int = 5) -> list[tuple[str, float]]:
        """observer 最偏好的 tag（供 Agent 生成「你可能也喜欢…」的自然语言解释）。"""
        w = self._weights.get(observer_id, {})
        ranked = sorted(w.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(t, round(v, 3)) for t, v in ranked if v > 1.0][:k]


# 全局单例，供 matching / main 复用
preference = PreferenceModel()
