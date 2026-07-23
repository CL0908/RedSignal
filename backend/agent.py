"""Agent 模块（PRD 第7章）。
- 只生成解释/破冰/纪念文案，不做任何权限或匹配判断（附录A规则6）；
- 输出必须过 schema 校验；超时(5s)/失败/无API key -> 预置文案；
- 预置文案 3 套轮换，保证 Demo 断网可用。"""
from __future__ import annotations

import itertools
import json
import os
from typing import Optional

import httpx

from . import config

FALLBACKS = itertools.cycle([
    {
        "connection_reason": "你们都在尝试让 AI 从屏幕进入现实设备。",
        "icebreaker": "你最想让哪一种现实物品拥有自己的 Agent？",
        "memory_caption": "两个按钮确认了一次本来可能错过的相遇。",
    },
    {
        "connection_reason": "你们对同一类问题感到兴奋，而且都来到了现场。",
        "icebreaker": "这次活动里你最想验证的一个想法是什么？",
        "memory_caption": "同一个现场，同一个信号，一次双向确认。",
    },
    {
        "connection_reason": "共同的兴趣把你们放进了同一个候选池，双击把它变成了连接。",
        "icebreaker": "如果只带一件设备来黑客松，你会带什么？",
        "memory_caption": "相遇不靠运气，靠两次双击。",
    },
])

_REQUIRED_KEYS = {"connection_reason", "icebreaker", "memory_caption"}

SYSTEM_PROMPT = (
    "你是线下社交产品 RedSignal 的破冰助手。根据输入的活动、共同兴趣和双方目标，"
    "输出严格的 JSON 对象，仅包含三个键：connection_reason（一句话解释为什么适合认识，"
    "不超过40字）、icebreaker（一个自然不冒犯、可直接开口的问题）、"
    "memory_caption（一句相遇纪念文案）。只输出 JSON，不要任何其他文字或代码块标记。"
)


def _validate(obj: dict) -> bool:
    return (
        isinstance(obj, dict)
        and _REQUIRED_KEYS <= set(obj.keys())
        and all(isinstance(obj[k], str) and obj[k].strip() for k in _REQUIRED_KEYS)
    )


def generate(payload: dict) -> dict:
    """同步生成。任何失败路径都返回 fallback，绝不抛异常。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return next(FALLBACKS)
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            timeout=config.AGENT_TIMEOUT_SECONDS,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.AGENT_MODEL,
                "max_tokens": 500,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            },
        )
        resp.raise_for_status()
        text = "".join(
            block.get("text", "")
            for block in resp.json().get("content", [])
            if block.get("type") == "text"
        )
        cleaned = text.replace("```json", "").replace("```", "").strip()
        obj = json.loads(cleaned)
        if _validate(obj):
            return {k: obj[k] for k in _REQUIRED_KEYS}
    except Exception:
        pass
    return next(FALLBACKS)


def build_payload(event_id: str, mode: str, shared_interests: list[str],
                  goal_a: str, goal_b: str, confirmation_method: str) -> dict:
    return {
        "event": event_id,
        "mode": mode,
        "shared_interests": shared_interests,
        "user_a_goal": goal_a,
        "user_b_goal": goal_b,
        "confirmation_method": confirmation_method,
    }
