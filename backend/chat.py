"""Encounter 内的聊天（PRD 5.7 之后的延伸）。

设计边界：
  - 会话只能存在于**已建立的 Encounter** 内。没有双向确认就没有聊天通道，
    这是产品红线在数据层的体现——不给"先聊聊看"留后门。
  - 路由键用 encounter_id，不用对方 user_id。前端始终不需要知道对方是谁，
    只有后端能把 encounter 解析成两个参与者（与 share_bundle 的隐私模型一致）。
  - 内存是唯一事实来源（读路径零延迟、可离线演示），落库走 persistence 异步镜像。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from .models import ChatMessage, now
from .persistence import persistence
from .store import store

log = logging.getLogger("redsignal.chat")

MAX_TEXT_LEN = 500          # 与前端 textarea maxlength 对齐
MAX_THREAD_LEN = 500        # 单场活动够用，防止内存无上限增长


class ChatError(Exception):
    """调用方不是该 encounter 的参与者，或 encounter 不存在。"""


class ChatStore:
    def __init__(self) -> None:
        self.threads: dict[str, list[ChatMessage]] = {}

    # ---- 参与者校验 ----
    def participants(self, encounter_id: str) -> tuple[str, str]:
        enc = store.encounters.get(encounter_id)
        if enc is None:
            raise ChatError(f"unknown encounter {encounter_id}")
        pair = store.get_pair(enc.pair_id)
        if pair is None:
            raise ChatError(f"encounter {encounter_id} has no pair")
        return pair.user_a, pair.user_b

    def partner_of(self, encounter_id: str, user_id: str) -> str:
        a, b = self.participants(encounter_id)
        if user_id == a:
            return b
        if user_id == b:
            return a
        raise ChatError(f"{user_id} not a participant of {encounter_id}")

    # ---- 收发 ----
    def append(self, encounter_id: str, sender_id: str, text: str) -> ChatMessage:
        self.partner_of(encounter_id, sender_id)      # 顺带校验参与者身份
        text = (text or "").strip()[:MAX_TEXT_LEN]
        if not text:
            raise ChatError("empty message")
        msg = ChatMessage(message_id=f"msg_{uuid.uuid4().hex[:12]}",
                          encounter_id=encounter_id, sender_id=sender_id, text=text)
        thread = self.threads.setdefault(encounter_id, [])
        thread.append(msg)
        if len(thread) > MAX_THREAD_LEN:
            del thread[:-MAX_THREAD_LEN]
        persistence.write("chat_messages", {
            "message_id": msg.message_id,
            "encounter_id": msg.encounter_id,
            "sender_id": msg.sender_id,
            "text": msg.text,
            "created_at": msg.created_at.isoformat(),
        }, on_conflict="message_id")
        return msg

    def history(self, encounter_id: str) -> list[ChatMessage]:
        return list(self.threads.get(encounter_id, []))

    def restore(self, encounter_id: str) -> list[ChatMessage]:
        """内存里没有就去 Supabase 捞回来。

        进程重启会清空内存（实测遇到过 Railway 的 1012 service restart），
        但消息其实一直在 chat_messages 表里。不回落的话，用户会看到
        "聊天记录凭空消失"——数据没丢，只是读不到，比真丢了更让人困惑。
        """
        if self.threads.get(encounter_id):
            return self.history(encounter_id)
        rows = persistence.read("chat_messages",
                                f"encounter_id=eq.{encounter_id}&order=created_at.asc")
        if not rows:
            return []
        restored = []
        for r in rows:
            try:
                restored.append(ChatMessage(
                    message_id=r["message_id"], encounter_id=r["encounter_id"],
                    sender_id=r["sender_id"], text=r["text"],
                    created_at=datetime.fromisoformat(
                        r["created_at"].replace("Z", "+00:00")),
                ))
            except Exception:      # noqa: BLE001 —— 单行坏数据不该拖垮整段历史
                continue
        if restored:
            self.threads[encounter_id] = restored
            log.info("从 Supabase 恢复会话 %s：%d 条", encounter_id, len(restored))
        return restored

    def as_payload(self, msg: ChatMessage, viewer_id: str) -> dict:
        """给前端的一条消息。mine 由后端判定，前端不需要知道对方 user_id。"""
        return {
            "type": "chat_message",
            "message_id": msg.message_id,
            "encounter_id": msg.encounter_id,
            "mine": msg.sender_id == viewer_id,
            "text": msg.text,
            "ts": msg.created_at.isoformat(),
        }

    def analyze_payload(self, encounter_id: str) -> list[dict]:
        """转成 agent.analyze_rapport / compute_engagement 要的格式。"""
        return [{"sender": m.sender_id,
                 "ts": m.created_at.timestamp(),
                 "text": m.text}
                for m in self.history(encounter_id)]

    def clear_user(self, user_id: str) -> None:
        for eid in list(self.threads):
            try:
                self.partner_of(eid, user_id)
            except ChatError:
                continue
            self.threads.pop(eid, None)


chat_store = ChatStore()
