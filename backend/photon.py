"""破冰官 —— 匹配确认后，让「有手机号的 Agent」主动给双方发 iMessage。

架构：本模块(Python) → HTTP → photon-agent(Node/spectrum-ts) → iMessage → 双方手机。
之所以要 Node：Photon 发消息只有 spectrum-ts(TS) SDK，没有 REST 发送接口。

破冰文案由 agent.generate（Claude，带 fallback）产出；本模块只负责投递。
photon-agent 未启动/失败时静默降级（返回 False），绝不影响主匹配/确认流程。
"""
from __future__ import annotations

import logging
import os

import httpx

from . import agent

log = logging.getLogger("photon")

PHOTON_AGENT_URL = os.environ.get("PHOTON_AGENT_URL", "http://localhost:8787")


def send_icebreak(recipients: list[str], text: str, group: bool = True) -> bool:
    """让破冰官给 recipients（手机号列表）发一条破冰消息。失败返回 False，不抛异常。"""
    if not recipients or not text:
        return False
    try:
        r = httpx.post(f"{PHOTON_AGENT_URL}/icebreak",
                       json={"recipients": recipients, "text": text, "group": group},
                       timeout=8)
        r.raise_for_status()
        data = r.json()
        log.info("破冰官已投递(%s%s): %s",
                 data.get("mode"), " · MOCK" if data.get("mock") else "", recipients)
        return bool(data.get("ok"))
    except Exception as e:
        log.warning("破冰官投递失败(photon-agent 未启动？): %s", e)
        return False


def build_icebreaker_text(event: str, mode: str, shared_interests: list[str],
                          goal_a: str = "", goal_b: str = "") -> str:
    """用 Claude 生成破冰文案（无 key 时走 agent 的确定性 fallback）。"""
    payload = agent.build_payload(event, mode, shared_interests, goal_a, goal_b, "dual_ring_button")
    out = agent.generate(payload)  # {connection_reason, icebreaker, memory_caption}
    shared = "、".join(shared_interests) if shared_interests else "刚在现场对上的信号"
    return (
        f"❤️ 你俩刚在现场匹配上了（双击戒指确认）。\n"
        f"共同点：{shared}。\n"
        f"{out.get('connection_reason', '')}\n"
        f"破冰问题：{out.get('icebreaker', '聊聊你最近在做什么？')}"
    )


def icebreak_pair(phones: list[str], event: str, mode: str,
                  shared_interests: list[str], goal_a: str = "", goal_b: str = "") -> bool:
    """一步到位：生成破冰文案 + 让破冰官发给双方。"""
    text = build_icebreaker_text(event, mode, shared_interests, goal_a, goal_b)
    return send_icebreak(phones, text, group=True)
