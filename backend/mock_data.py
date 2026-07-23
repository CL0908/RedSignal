"""预置演示用户（PRD 第19章赛前清单）。

u_demo_a / u_demo_b : 主 Demo 用的高分绿色对
u_demo_red          : 模式不兼容对照组（验证 T01）
d01..d07            : 舞池密集场景测试用（验证冷却、排序、稳定配对）
"""
from __future__ import annotations

from . import config
from .models import Mode, UserEventProfile
from .store import store

EV = config.DEFAULT_EVENT_ID


def _u(uid, nick, goal, tags_, style="deep_small_group", bundle=None, mode=Mode.OFF):
    return UserEventProfile(
        user_id=uid, event_id=EV, mode=mode, social_goal=goal,
        interest_tags=tags_, communication_style=style,
        share_bundle=bundle or {}, nickname=nick,
    )


DEMO_USERS = {
    # ---- 主 Demo 对：故意用不同写法的同义词，展示归一化效果 ----
    "u_demo_a": _u("u_demo_a", "信号狐", "project_teammate",
                   ["AI Agent", "智能硬件", "科幻电影"],
                   bundle={"wechat": "fox_demo_a", "github": "foxbuilds",
                           "bio": "在做 AI 硬件产品"}),
    "u_demo_b": _u("u_demo_b", "夜航鲸", "project_teammate",
                   ["人工智能代理", "可穿戴设备", "电子音乐"],
                   bundle={"wechat": "whale_demo_b", "team_need": "找硬件方向队友"}),
    # ---- 模式不兼容对照 ----
    "u_demo_red": _u("u_demo_red", "晚风", "romance",
                     ["摄影", "徒步"], style="one_on_one"),

    # ---- 舞池人群 ----
    "d01": _u("d01", "跳电", "hobby_friend", ["电子音乐", "蹦迪", "摄影"],
              style="casual_large_group", bundle={"instagram": "d01"}),
    "d02": _u("d02", "低频", "hobby_friend", ["techno", "clubbing", "拍照"],
              style="casual_large_group", bundle={"instagram": "d02"}),
    "d03": _u("d03", "回声", "event_buddy", ["EDM", "电音", "旅行"],
              style="casual_large_group"),
    "d04": _u("d04", "折射", "project_teammate", ["LLM", "大模型", "创业"],
              bundle={"github": "d04"}),
    "d05": _u("d05", "缓存", "project_teammate", ["大语言模型", "独立开发", "前端"],
              bundle={"github": "d05"}),
    "d06": _u("d06", "南极", "long_term_friend", ["读书", "电影", "咖啡"],
              style="one_on_one"),
    "d07": _u("d07", "热带", "long_term_friend", ["阅读", "看片", "手冲"],
              style="one_on_one"),
}

EPHEMERAL_IDS = {uid: f"eph_{uid}" for uid in DEMO_USERS}


def load() -> None:
    for uid, profile in DEMO_USERS.items():
        store.upsert_profile(profile)
        store.register_ephemeral(EPHEMERAL_IDS[uid], uid)
