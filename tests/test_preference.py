"""偏好学习层测试：engagement 计算 + 权重更新 + 排序加分（全确定性）。"""
from backend import agent
from backend.preference import (
    PreferenceModel,
    compute_engagement,
    PREF_BONUS_MAX,
)


def _chat(pairs):
    """pairs: [(sender, ts, text), ...] -> messages 列表。"""
    return [{"sender": s, "ts": t, "text": x} for s, t, x in pairs]


def test_engagement_empty_is_zero():
    m = compute_engagement([], rapport=0.9)
    assert m.engagement == 0.0
    assert m.message_count == 0


def test_engagement_rises_with_activity():
    # 冷淡：2 条、慢回复、同一天
    cold = compute_engagement(_chat([
        ("a", 0, "hi"), ("b", 3000, "hey"),
    ]), rapport=0.3)
    # 热络：多条、快回复、跨 3 天、高 rapport
    day = 86400
    msgs = []
    for d in range(3):
        for i in range(6):
            sender = "a" if i % 2 == 0 else "b"
            msgs.append((sender, d * day + i * 20, f"m{i}"))
    hot = compute_engagement(_chat(msgs), rapport=0.9)
    assert hot.engagement > cold.engagement
    assert hot.active_days == 3
    assert 0.0 <= hot.engagement <= 1.0


def test_reply_speed_faster_is_higher():
    fast = compute_engagement(_chat([("a", 0, "x"), ("b", 10, "y")]), rapport=0.5)
    slow = compute_engagement(_chat([("a", 0, "x"), ("b", 290, "y")]), rapport=0.5)
    assert fast.reply_speed_score > slow.reply_speed_score


def test_preference_update_and_bonus_direction():
    pm = PreferenceModel()
    # 没学过任何东西 → 加分为 0（不影响排序）
    assert pm.preference_bonus("u1", ["ai-agent"]) == 0.0

    # 和「摄影 + AI」的人聊得很好 → 上调这些 tag
    pm.update_from_chat("u1", ["摄影", "AI Agent"], engagement=0.9)
    good = pm.preference_bonus("u1", ["摄影"])
    assert 0.0 < good <= PREF_BONUS_MAX

    # 和「游戏」的人聊得很差 → 不上调；对游戏类不给正加分
    pm.update_from_chat("u1", ["游戏"], engagement=0.1)
    assert pm.preference_bonus("u1", ["gaming"]) == 0.0


def test_preference_bonus_scales_with_repeated_positive_chats():
    pm = PreferenceModel()
    pm.update_from_chat("u1", ["摄影"], engagement=0.8)
    once = pm.preference_bonus("u1", ["摄影"])
    for _ in range(5):
        pm.update_from_chat("u1", ["摄影"], engagement=0.8)
    many = pm.preference_bonus("u1", ["摄影"])
    assert many >= once  # 反复深聊 → 偏好增强（有上限）


def test_top_tags_reports_learned_preferences():
    pm = PreferenceModel()
    pm.update_from_chat("u1", ["摄影"], engagement=0.95)
    pm.update_from_chat("u1", ["AI Agent"], engagement=0.7)
    top = pm.top_tags("u1")
    tags_only = [t for t, _ in top]
    assert "photography" in tags_only or "摄影" in tags_only  # 归一化后
    assert all(w > 1.0 for _, w in top)


# ---- Agent 断网 fallback（无 ANTHROPIC_API_KEY 时也要可用）----
def test_extract_labels_fallback_scans_synonyms(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    labels = agent.extract_labels("我平时喜欢摄影和电子音乐，也在做创业")
    assert "photography" in labels
    assert "music-electronic" in labels
    assert "startup" in labels


def test_analyze_rapport_fallback_is_neutral(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = agent.analyze_rapport(_chat([("a", 0, "hi"), ("b", 5, "hey")]))
    assert r["rapport"] == 0.5


def test_explain_preference_fallback_template(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = agent.explain_preference([("photography", 1.4), ("ai-agent", 1.2)])
    assert isinstance(s, str) and len(s) > 0
