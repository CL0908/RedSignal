"""分享包交换（PRD 5.6 / 11.1）。确定性代码，无任何临时扩大范围的路径。
- 只交换用户预先授权且在白名单内的字段；
- FORBIDDEN_FIELDS 无条件剔除，即使误存进了 profile；
- 共同兴趣由系统计算，属于必选字段。"""
from __future__ import annotations

from . import tags
from .models import FORBIDDEN_FIELDS, OPTIONAL_FIELDS, REQUIRED_FIELDS
from .store import store

_ALLOWED = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)


def _visible_card(owner_id: str, viewer_id: str) -> dict[str, object]:
    """构造 viewer 能看到的 owner 社交卡。"""
    owner = store.get_profile(owner_id)
    viewer = store.get_profile(viewer_id)
    assert owner is not None and viewer is not None

    card: dict[str, object] = {"nickname": owner.nickname}
    # 用归一化后的标签取交集，否则 "AI Agent" 与 "人工智能代理" 显示不出共同点
    card["shared_interests"] = tags.display_shared(owner.interest_tags,
                                                   viewer.interest_tags)

    for field_name, value in owner.share_bundle.items():
        if field_name in FORBIDDEN_FIELDS:
            continue                      # 硬性剔除
        if field_name not in _ALLOWED:
            continue                      # 白名单外一律不交换
        card[field_name] = value
    return card


def exchange(user_a: str, user_b: str) -> dict[str, dict[str, object]]:
    """返回 {接收方user_id: 其可见的对方卡片}。"""
    return {
        user_a: _visible_card(owner_id=user_b, viewer_id=user_a),
        user_b: _visible_card(owner_id=user_a, viewer_id=user_b),
    }
