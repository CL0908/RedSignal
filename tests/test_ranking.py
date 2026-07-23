"""推荐排序引擎测试：归一化、排序、两层冷却、密集场景、稳定配对。"""
from datetime import timedelta

import pytest

from backend import config, confirm, matching, presence, tags
from backend.models import Mode, SessionState, UserEventProfile, now
from backend.state_machine import set_mode
from backend.store import Store
import backend.store as store_module

S = SessionState


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    st = Store()
    import backend.state_machine as sm
    import backend.share_bundle as sb
    for mod in (store_module, matching, presence, confirm, sm, sb):
        monkeypatch.setattr(mod, "store", st, raising=True)
    confirm.reset()
    yield st


def mk(st, uid, goal="project_teammate", tags_=None, style="deep_small_group",
       mode=Mode.FRIEND):
    p = UserEventProfile(
        user_id=uid, event_id=config.DEFAULT_EVENT_ID, mode=mode,
        social_goal=goal, interest_tags=tags_ or ["ai-agent", "hw-wearable", "sci-fi"],
        communication_style=style, share_bundle={}, nickname=uid,
    )
    st.upsert_profile(p)
    st.register_ephemeral(f"eph_{uid}", uid)
    set_mode(uid, mode)
    return p


def sight(observer, target_uid, times=None, rssi=-60):
    """模拟连续采样。"""
    n = times or config.PRESENCE_MIN_SIGHTINGS
    result = (None, "")
    for _ in range(n):
        result = presence.report_sighting(observer, f"eph_{target_uid}", rssi)
    return result


# ---------------- 同义词归一化 ----------------
def test_synonyms_fold_to_same_tag():
    assert tags.normalize_tag("AI Agent") == "ai-agent"
    assert tags.normalize_tag("人工智能代理") == "ai-agent"
    assert tags.normalize_tag("智能体") == "ai-agent"
    assert tags.normalize_tag("智能硬件") == "hw-wearable"
    assert tags.normalize_tag("可穿戴设备") == "hw-wearable"


def test_unknown_tag_is_kept_not_dropped():
    """表里没有的标签保留下来，不能丢用户输入。"""
    out = tags.normalize_tags(["量子编织", "AI Agent"])
    assert "ai-agent" in out
    assert len(out) == 2


def test_normalization_lifts_score(fresh):
    """归一化前算不出重合，归一化后能算出来。"""
    st = fresh
    a = mk(st, "ua", tags_=["AI Agent", "智能硬件", "科幻电影"])
    b = mk(st, "ub", tags_=["人工智能代理", "可穿戴设备", "赛博朋克"])
    # 原始标签零重合
    assert matching.jaccard(a.interest_tags, b.interest_tags) == 0.0
    # 归一化后重合
    assert matching.jaccard(tags.normalize_tags(a.interest_tags),
                            tags.normalize_tags(b.interest_tags)) > 0.5
    assert matching.compat_score(a, b) >= config.MATCH_SCORE_THRESHOLD


# ---------------- 分数对称性（稳定匹配的前提）----------------
def test_score_is_symmetric(fresh):
    st = fresh
    a = mk(st, "ua", tags_=["ai-agent", "llm"])
    b = mk(st, "ub", goal="industry_chat", tags_=["ai-agent", "startup"],
           style="one_on_one")
    assert matching.compat_score(a, b) == matching.compat_score(b, a)


# ---------------- 排序：推分最高的，不是先发现的 ----------------
def test_ranks_by_score_not_discovery_order(fresh):
    """两人都够格时，排前面的是分高的，不是先被发现的。"""
    st = fresh
    mk(st, "me", tags_=["ai-agent", "llm", "startup"])
    # lower: 兴趣重合 50%，其余全同 -> 恰好 80 分（刚过线）
    mk(st, "lower", tags_=["ai-agent", "llm", "robotics"])
    # higher: 完全一致 -> 100 分
    mk(st, "higher", tags_=["ai-agent", "llm", "startup"])

    assert matching.compat_score(st.get_profile("me"), st.get_profile("lower")) == 80
    assert matching.compat_score(st.get_profile("me"), st.get_profile("higher")) == 100

    # 直接注入观测（绕过 report_sighting，避免中途就锁定配对）
    base = now()
    for i in range(config.PRESENCE_MIN_SIGHTINGS):
        st.add_sighting(_s("me", "eph_lower", -60, base + timedelta(seconds=i)))
    for i in range(config.PRESENCE_MIN_SIGHTINGS):
        st.add_sighting(_s("me", "eph_higher", -60, base + timedelta(seconds=i + 5)))

    ranked, _ = matching.collect_candidates("me")
    assert [c.user_id for c in ranked] == ["higher", "lower"], "应按分数排序而非发现顺序"


def test_dwell_breaks_ties(fresh):
    """兼容分相同时，停留久的排前面。"""
    st = fresh
    mk(st, "me")
    mk(st, "passerby")
    mk(st, "stayer")

    base = now()
    # 两人兼容分完全相同
    for i in range(config.PRESENCE_MIN_SIGHTINGS):
        st.add_sighting(_s("me", "eph_passerby", -60, base + timedelta(seconds=i * 0.5)))
        st.add_sighting(_s("me", "eph_stayer", -60, base + timedelta(seconds=i * 20)))

    ranked, _ = matching.collect_candidates("me")
    assert ranked[0].user_id == "stayer"
    assert ranked[0].compat_score == ranked[1].compat_score  # 兼容分相同
    assert ranked[0].rank_score > ranked[1].rank_score       # 排序分不同


def _s(observer, eph, rssi, at):
    from backend.models import Sighting
    return Sighting(observer_user_id=observer, ephemeral_id=eph, rssi=rssi, seen_at=at)


# ---------------- 舞池：核心场景 ----------------
def test_dancefloor_pushes_one_then_goes_quiet(fresh):
    """密集场景：一次只推一个，之后进入静默期，不再被轰炸。"""
    st = fresh
    mk(st, "me", tags_=["ai-agent", "llm", "startup"])
    for i in range(6):
        mk(st, f"p{i}", tags_=["ai-agent", "llm", "startup"])

    pair, reason = sight("me", "p0")
    assert pair is not None and reason == "matched"

    # 舞池里其他人继续被扫到，但 me 已在静默期
    for i in range(1, 6):
        p2, r2 = sight("me", f"p{i}")
        assert p2 is None, f"静默期内不应再推送（p{i}）"
        assert r2 == "observer_not_eligible"


def test_after_cooldown_pushes_a_different_person(fresh):
    """冷却结束后，推的是不同的人（同一对不重复）。"""
    st = fresh
    mk(st, "me", tags_=["ai-agent", "llm", "startup"])
    for i in range(4):
        mk(st, f"p{i}", tags_=["ai-agent", "llm", "startup"])

    pair1, _ = sight("me", "p0")
    assert pair1 is not None
    first_partner = pair1.user_b

    # 第一次没成：候选过期 + 双方解除静默（模拟 10 分钟后）
    confirm.expire_pair(pair1.pair_id)
    st.user_quiet_until.clear()
    for uid in ("me", first_partner):
        st.set_state(uid, S.DISCOVERABLE)

    # 重新扫描全场
    for i in range(4):
        sight("me", f"p{i}")

    pair2 = st.active_pair_for("me")
    assert pair2 is not None, "冷却后应能收到新提醒"
    assert pair2.user_b != first_partner, "不应重复推荐同一个人"


def test_pair_cooldown_blocks_repeat(fresh):
    """同一对推过没成，整场活动内不再重复推荐。"""
    st = fresh
    mk(st, "me")
    mk(st, "you")
    pair, _ = sight("me", "you")
    assert pair is not None

    confirm.expire_pair(pair.pair_id)
    st.user_quiet_until.clear()
    for uid in ("me", "you"):
        st.set_state(uid, S.DISCOVERABLE)

    p2, reason = sight("me", "you")
    assert p2 is None
    assert reason == "pair_cooldown"


def test_busy_partner_is_skipped(fresh):
    """最优候选正在别的配对中时，顺延到下一个，而不是失败。"""
    st = fresh
    mk(st, "me", tags_=["ai-agent", "llm", "startup"])
    mk(st, "best", tags_=["ai-agent", "llm", "startup"])
    mk(st, "second", goal="industry_chat", tags_=["ai-agent", "llm", "startup"])
    mk(st, "other", tags_=["ai-agent", "llm", "startup"])

    # best 先和 other 配上了
    sight("other", "best")
    assert st.active_pair_for("best") is not None

    for uid in ("second",):
        pass
    for _ in range(config.PRESENCE_MIN_SIGHTINGS):
        presence.report_sighting("me", "eph_best", -60)
        presence.report_sighting("me", "eph_second", -60)

    pair = st.active_pair_for("me")
    assert pair is not None
    assert pair.user_b == "second", "应跳过忙碌的最优候选"


# ---------------- 全局稳定配对 ----------------
def test_stable_round_no_blocking_pair(fresh):
    """一轮全局撮合后，不存在'两人互相更想要对方却被拆开'的情况。"""
    st = fresh
    people = {
        "x1": ["ai-agent", "llm", "startup"],
        "x2": ["ai-agent", "llm", "startup"],
        "x3": ["music-electronic", "photography", "travel"],
        "x4": ["music-electronic", "photography", "travel"],
    }
    for uid, t in people.items():
        mk(st, uid, tags_=t)
    # 全员互相可见
    for a in people:
        for b in people:
            if a != b:
                for _ in range(config.PRESENCE_MIN_SIGHTINGS):
                    presence.report_sighting(a, f"eph_{b}", -60)
        st.user_quiet_until.clear()
        for uid in people:
            if st.get_state(uid) == S.NOTIFIED:
                st.set_state(uid, S.DISCOVERABLE)
        for p in list(st.pairs.values()):
            p.cancelled = True
    st.pair_tried_at.clear()
    st.user_quiet_until.clear()
    for uid in people:
        st.set_state(uid, S.DISCOVERABLE)
    for p in st.pairs.values():
        p.cancelled = True

    pairs = matching.stable_round(config.DEFAULT_EVENT_ID)
    assert len(pairs) == 2, "四个人应配成两对"
    matched = {frozenset((p.user_a, p.user_b)) for p in pairs}
    # 同兴趣的应该配在一起
    assert frozenset(("x1", "x2")) in matched
    assert frozenset(("x3", "x4")) in matched


def test_blue_user_never_in_candidates(fresh):
    """蓝色用户不进候选池。"""
    st = fresh
    mk(st, "me")
    mk(st, "bluey")
    set_mode("bluey", Mode.OFF)
    for _ in range(config.PRESENCE_MIN_SIGHTINGS):
        presence.report_sighting("me", "eph_bluey", -60)
    ranked, rejected = matching.collect_candidates("me")
    assert ranked == []
    assert rejected["bluey"] == "blue_mode"


# ---------------- 领域部分分 ----------------
def test_domain_credit_connects_adjacent_concepts(fresh):
    """ai-agent 与 llm 字面不重合，但同属 ai 领域，应拿到部分分。"""
    st = fresh
    a = mk(st, "agent_dev", tags_=["AI Agent", "智能硬件", "科幻电影"])
    b = mk(st, "llm_dev", goal="industry_chat", tags_=["LLM", "大模型", "创业"])
    assert matching.jaccard(tags.normalize_tags(a.interest_tags),
                            tags.normalize_tags(b.interest_tags)) == 0.0
    assert tags.soft_overlap(a.interest_tags, b.interest_tags) > 0.0


def test_unrelated_domains_get_no_credit(fresh):
    st = fresh
    a = mk(st, "coder", tags_=["ai-agent", "llm"])
    b = mk(st, "climber", tags_=["fitness", "outdoor"])
    assert tags.soft_overlap(a.interest_tags, b.interest_tags) == 0.0


def test_soft_overlap_never_exceeds_one(fresh):
    st = fresh
    a = mk(st, "p1", tags_=["ai-agent", "llm", "robotics"])
    b = mk(st, "p2", tags_=["ai-agent", "llm", "robotics"])
    assert tags.soft_overlap(a.interest_tags, b.interest_tags) == 1.0
