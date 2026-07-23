"""主路径测试，覆盖 PRD 15.1 的 T01-T09 关键场景。"""
from datetime import timedelta

import pytest

from backend import config, confirm, matching, presence
from backend.models import (
    ButtonEventType, Mode, RingButtonEvent, SessionState, UserEventProfile, now,
)
from backend.state_machine import go_blue, set_mode
from backend.store import Store
import backend.store as store_module

S = SessionState


def make_user(uid: str, mode: Mode = Mode.FRIEND, goal: str = "project_teammate",
              tags=None, style: str = "deep_small_group",
              bundle=None, nickname: str = "u") -> UserEventProfile:
    return UserEventProfile(
        user_id=uid, event_id=config.DEFAULT_EVENT_ID, mode=mode,
        social_goal=goal, interest_tags=tags or ["ai-agent", "wearable", "sci-fi"],
        communication_style=style, share_bundle=bundle or {}, nickname=nickname,
    )


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    """每个测试用干净的 store + 重置确认器。"""
    st = Store()
    for mod in (store_module, matching, presence, confirm):
        monkeypatch.setattr(mod, "store", st, raising=True)
    import backend.state_machine as sm
    import backend.share_bundle as sb
    monkeypatch.setattr(sm, "store", st, raising=True)
    monkeypatch.setattr(sb, "store", st, raising=True)
    confirm.reset()
    yield st


def setup_pair(st, tags_b=None, goal_b="project_teammate"):
    """标准两用户 + 走完持续性发现，返回 pair。"""
    a = make_user("ua", bundle={"wechat": "wx_a"})
    b = make_user("ub", tags=tags_b, goal=goal_b, bundle={"wechat": "wx_b"})
    st.upsert_profile(a)
    st.upsert_profile(b)
    st.register_ephemeral("eph_b", "ub")
    set_mode("ua", Mode.FRIEND)
    set_mode("ub", Mode.FRIEND)
    pair = None
    for _ in range(config.PRESENCE_MIN_SIGHTINGS):
        pair, reason = presence.report_sighting("ua", "eph_b", -60)
    return pair, reason


def press(uid: str, pair_id: str, at=None) -> confirm.ConfirmResult:
    ev = RingButtonEvent(user_id=uid, pair_id=pair_id,
                         event_type=ButtonEventType.DOUBLE_PRESS_CONFIRM,
                         detected_at=at or now())
    return confirm.handle_button_event(ev)


# ---------- T01 模式不同：不匹配 ----------
def test_t01_mode_mismatch(fresh_store):
    st = fresh_store
    a = make_user("ua")
    b = make_user("ub", mode=Mode.LOVE)
    st.upsert_profile(a); st.upsert_profile(b)
    st.register_ephemeral("eph_b", "ub")
    set_mode("ua", Mode.FRIEND)
    set_mode("ub", Mode.LOVE)
    for _ in range(config.PRESENCE_MIN_SIGHTINGS):
        pair, reason = presence.report_sighting("ua", "eph_b", -60)
    assert pair is None
    assert reason == "mode_mismatch"


# ---------- T02 分数不足：不提醒 ----------
def test_t02_score_below_threshold(fresh_store):
    pair, reason = setup_pair(fresh_store,
                              tags_b=["cooking", "gardening", "chess"],
                              goal_b="event_buddy")
    assert pair is None
    assert reason.startswith("score_below_threshold")


# ---------- T03 匹配且持续在附近：双方进入 NOTIFIED ----------
def test_t03_match_and_notify(fresh_store):
    st = fresh_store
    pair, reason = setup_pair(st)
    assert pair is not None and reason == "matched"
    assert pair.match_score >= config.MATCH_SCORE_THRESHOLD
    assert st.get_state("ua") == S.NOTIFIED
    assert st.get_state("ub") == S.NOTIFIED


# ---------- 持续性：不足采样次数不触发（防信号波动） ----------
def test_persistence_requires_min_sightings(fresh_store):
    st = fresh_store
    a = make_user("ua"); b = make_user("ub")
    st.upsert_profile(a); st.upsert_profile(b)
    st.register_ephemeral("eph_b", "ub")
    set_mode("ua", Mode.FRIEND); set_mode("ub", Mode.FRIEND)
    pair, reason = presence.report_sighting("ua", "eph_b", -60)
    assert pair is None and reason.startswith("need_more_sightings")


# ---------- T04 只有 A 确认：不交换，窗口后静默失效 ----------
def test_t04_single_confirm_expires_silently(fresh_store):
    st = fresh_store
    pair, _ = setup_pair(st)
    r = press("ua", pair.pair_id)
    assert r.status == "accepted"
    assert st.get_state("ua") == S.SELF_CONFIRMED
    assert st.encounters == {}
    # 模拟窗口超时
    confirm._window_deadline[pair.pair_id] = now() - timedelta(seconds=1)
    assert confirm.check_window_expiry(pair.pair_id) is True
    assert st.encounters == {}
    assert st.get_state("ua") == S.DISCOVERABLE
    assert st.get_state("ub") == S.DISCOVERABLE


# ---------- T05 双方窗口内确认：建立 Encounter，只交换预授权字段 ----------
def test_t05_dual_confirm_creates_encounter(fresh_store):
    st = fresh_store
    pair, _ = setup_pair(st)
    assert press("ua", pair.pair_id).status == "accepted"
    r = press("ub", pair.pair_id)
    assert r.status == "encounter_created"
    enc = r.encounter
    assert set(enc.confirmed_by) == {"ua", "ub"}
    # ua 看到 ub 的卡：含昵称、共同兴趣、ub 授权的微信
    card_for_ua = enc.shared_fields["ua"]
    assert card_for_ua["nickname"] == "u"
    assert "ai-agent" in card_for_ua["shared_interests"]
    assert card_for_ua["wechat"] == "wx_b"
    assert st.get_state("ua") == S.CONNECTED
    assert st.get_state("ub") == S.CONNECTED


# ---------- T06 确认前一方切蓝：立即取消 ----------
def test_t06_blue_cancels(fresh_store):
    st = fresh_store
    pair, _ = setup_pair(st)
    press("ua", pair.pair_id)
    go_blue("ub")
    assert pair.cancelled is True
    # 此后 ua 的按钮事件不建立连接
    r = press("ua", pair.pair_id)
    assert r.status in ("pair_dead", "duplicate")
    assert st.encounters == {}
    assert st.get_state("ub") == S.BLUE_OFFLINE


# ---------- T07 未授权字段不交换 + 禁止字段硬剔除 ----------
def test_t07_share_bundle_respects_authorization(fresh_store):
    st = fresh_store
    a = make_user("ua", bundle={"github": "gh_a"})            # 未授权微信
    b = make_user("ub", bundle={"wechat": "wx_b", "phone": "13800000000"})  # phone 禁止
    st.upsert_profile(a); st.upsert_profile(b)
    st.register_ephemeral("eph_b", "ub")
    set_mode("ua", Mode.FRIEND); set_mode("ub", Mode.FRIEND)
    for _ in range(config.PRESENCE_MIN_SIGHTINGS):
        pair, _ = presence.report_sighting("ua", "eph_b", -60)
    press("ua", pair.pair_id)
    r = press("ub", pair.pair_id)
    enc = r.encounter
    assert "wechat" not in enc.shared_fields["ub"]   # ub 看 a 的卡：a 没授权微信
    assert enc.shared_fields["ub"]["github"] == "gh_a"
    assert "phone" not in enc.shared_fields["ua"]    # 禁止字段被剔除
    assert enc.shared_fields["ua"]["wechat"] == "wx_b"


# ---------- T08 候选过期后按按钮：不建立连接 ----------
def test_t08_expired_pair_button_ignored(fresh_store):
    st = fresh_store
    pair, _ = setup_pair(st)
    pair.candidate_expires_at = now() - timedelta(seconds=1)
    r = press("ua", pair.pair_id)
    assert r.status == "pair_dead"
    assert st.encounters == {}


# ---------- T09 Agent 失败/无 key：使用预置文案 ----------
def test_t09_agent_fallback(monkeypatch):
    from backend import agent
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = agent.generate({"event": "x"})
    assert set(out.keys()) == {"connection_reason", "icebreaker", "memory_caption"}
    assert all(out[k] for k in out)


# ---------- 防抖：3秒内重复双击只记一次 ----------
def test_debounce(fresh_store):
    pair, _ = setup_pair(fresh_store)
    t0 = now()
    assert press("ua", pair.pair_id, at=t0).status == "accepted"
    assert press("ua", pair.pair_id, at=t0 + timedelta(seconds=1)).status == "duplicate"
    # 超过防抖窗口的重复确认不改变结果（仍是单方）
    r = press("ua", pair.pair_id, at=t0 + timedelta(seconds=5))
    assert r.status == "accepted"
    assert fresh_store.encounters == {}


# ---------- 冷却：pair 过期后同一对用户短期内不再提醒 ----------
def test_notify_cooldown_two_layers(fresh_store):
    """两层冷却：用户级静默期优先生效；解除后人对级冷却仍拦住同一对。"""
    st = fresh_store
    pair, _ = setup_pair(st)
    confirm.expire_pair(pair.pair_id)

    # 第一层：ua 刚被提醒过，处于静默期
    pair2, reason = presence.report_sighting("ua", "eph_b", -60)
    assert pair2 is None
    assert reason == "observer_not_eligible"

    # 解除静默期后，第二层（同一对不重复推荐）仍然生效
    st.user_quiet_until.clear()
    for uid in ("ua", "ub"):
        st.set_state(uid, S.DISCOVERABLE)
    pair3, reason3 = presence.report_sighting("ua", "eph_b", -60)
    assert pair3 is None
    assert reason3 == "pair_cooldown"
