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


# ---------------------------------------------------------------------------
# 通用 Claude 调用（供下面三个 Agent 复用）——超时/无 key 一律返回 None，绝不抛异常
# ---------------------------------------------------------------------------
def _call_claude(system: str, user: str, model: Optional[str] = None,
                 max_tokens: int = 400) -> Optional[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
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
                "model": model or config.AGENT_MODEL,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        resp.raise_for_status()
        return "".join(
            b.get("text", "") for b in resp.json().get("content", [])
            if b.get("type") == "text"
        ).strip()
    except Exception:
        return None


def _extract_json(text: str):
    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Agent 1：标签抽取 —— 自由文本自我介绍 → 规范化 interest tags（匹配的种子）
#   录入阶段用一次；匹配路径本身不含 LLM（红线）。无 key 时退化为关键词扫描。
# ---------------------------------------------------------------------------
_LABEL_SYSTEM = (
    "你是线下社交产品的资料录入助手。用户会给一段自我介绍或兴趣描述，"
    "请抽取 3-8 个简短的兴趣/领域标签（中文或英文短词，如 'AI Agent'、'摄影'、'创业'）。"
    "只输出 JSON 数组，例如 [\"AI Agent\",\"摄影\",\"电子音乐\"]，不要任何其它文字。"
)


def extract_labels(intro_text: str) -> list[str]:
    """自我介绍 → 规范化标签列表。LLM 优先，失败退化为同义词表关键词扫描。"""
    from . import tags
    raw: list[str] = []
    out = _call_claude(_LABEL_SYSTEM, intro_text or "", model=config.AGENT_MODEL, max_tokens=200)
    if out:
        parsed = _extract_json(out)
        if isinstance(parsed, list):
            raw = [str(x) for x in parsed if str(x).strip()]
    if not raw:
        # fallback：扫描同义词表里出现过的写法（确定性，断网可用）
        low = (intro_text or "").lower()
        hit: list[str] = []
        for canon, variants in tags.SYNONYMS.items():
            if canon in low or any(v in low for v in variants):
                hit.append(canon)
        raw = hit
    return tags.normalize_tags(raw)


# ---------------------------------------------------------------------------
# Agent 2：聊天融洽度评分 —— 只出一个 0-1 的软信号 rapport，喂给 preference 学习
#   频率/回复速度/天数是确定性的（preference.compute_engagement 里算），这里只补「聊得好不好」。
#   高频调用 → 用便宜的 haiku。无 key 时返回中性 0.5。
# ---------------------------------------------------------------------------
_RAPPORT_SYSTEM = (
    "你是社交互动分析器。给你一段两人聊天记录，评估他们的融洽/投契程度。"
    "只输出 JSON：{\"rapport\": 0.0-1.0, \"reason\": \"一句话\"}。"
    "rapport 高=双向、热情、有来有回、想继续；低=冷淡、单向、敷衍。只输出 JSON。"
)


def analyze_rapport(messages: list[dict]) -> dict:
    """聊天记录 → {'rapport': float, 'reason': str}。无 key/失败 → 中性 0.5。"""
    if not messages:
        return {"rapport": 0.5, "reason": "无聊天记录"}
    transcript = "\n".join(
        f"{m.get('sender','?')}: {m.get('text','')}" for m in messages[-40:]
    )
    out = _call_claude(_RAPPORT_SYSTEM, transcript, model=config.AGENT_MODEL_FAST, max_tokens=150)
    if out:
        parsed = _extract_json(out)
        if isinstance(parsed, dict) and "rapport" in parsed:
            try:
                r = max(0.0, min(1.0, float(parsed["rapport"])))
                return {"rapport": r, "reason": str(parsed.get("reason", ""))}
            except (TypeError, ValueError):
                pass
    return {"rapport": 0.5, "reason": "评分不可用，取中性值"}


# ---------------------------------------------------------------------------
# Agent 3：偏好解释 —— 把 preference.top_tags 学到的权重翻成一句自然语言
#   纯展示用；无 key 时用模板兜底。
# ---------------------------------------------------------------------------
def explain_preference(top_tags: list[tuple[str, float]]) -> str:
    """[(tag, weight), ...] → 一句「你可能也喜欢…」。无 key → 模板。"""
    if not top_tags:
        return "还在了解你的偏好，多聊几次就能更懂你。"
    tag_str = "、".join(t for t, _ in top_tags[:4])
    out = _call_claude(
        "你是社交产品的洞察助手。根据用户最常深聊的兴趣标签，用一句温暖、不油腻的话"
        "总结「他可能也会喜欢认识哪类人」。只输出这一句话。",
        f"该用户深聊得来的人常有这些标签：{tag_str}",
        model=config.AGENT_MODEL, max_tokens=120,
    )
    return out or f"你和「{tag_str}」这类人往往聊得来，接下来会多为你留意他们。"
