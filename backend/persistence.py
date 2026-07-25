"""Supabase 写回层（write-behind）。

设计原则——为什么不是"把 store.py 换成 Supabase"：
  BLE sighting 在密集场景是每秒数十次的热路径，每次一个 HTTP 往返会让 demo 卡死。
  所以内存仍是唯一事实来源（读路径完全不变、零延迟、可离线演示），
  这里只把**值得留档的四类写入**异步镜像到 Postgres：
      profiles / candidate_pairs / ring_button_events / encounters
  IMU 与 sightings 不落库（models.py::IMUBatch 明确"只留内存，不入库"）。

失败语义：任何异常只记日志，绝不冒泡进业务逻辑。没配 .env 时整层关闭，
断网演示与现有测试完全不受影响。

顺序保证：单 worker 串行消费 FIFO 队列。encounters / ring_button_events
对 candidate_pairs 有外键，串行消费才能保证父行先落地。

配置（redsignal/.env，已 gitignore）：
  SUPABASE_URL=https://<ref>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY=eyJ...     # 只在后端用，绝不进前端/Git
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("redsignal.persistence")

_MAX_QUEUE = 10_000          # 满了丢最旧的，绝不无限增长
_TIMEOUT = 5.0


def _load_env() -> dict:
    """os.environ 优先，其次 redsignal/.env（与 Ditto 的 supabase_ingest 同款）。"""
    env = dict(os.environ)
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    return env


class Persistence:
    def __init__(self) -> None:
        env = _load_env()
        self.url = env.get("SUPABASE_URL", "").rstrip("/")
        self.key = env.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self.enabled = bool(self.url and self.key)
        self._queue: deque[tuple] = deque(maxlen=_MAX_QUEUE)
        self._worker: Optional[asyncio.Task] = None
        self._dropped = 0
        if not self.enabled:
            log.info("persistence disabled (缺 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")

    # ---------------- 入队（同步，永不阻塞调用方） ----------------

    def write(self, table: str, row: dict, on_conflict: str | None = None) -> None:
        """插入或 upsert 一行。on_conflict 给出冲突列则走 merge-duplicates。"""
        if not self.enabled:
            return
        if len(self._queue) == self._queue.maxlen:
            self._dropped += 1
        self._queue.append(("write", table, row, on_conflict, None))
        self._wake()

    def patch(self, table: str, filters: str, row: dict) -> None:
        """按 PostgREST 过滤串更新，例如 filters='user_id=eq.x&event_id=eq.y'。"""
        if not self.enabled:
            return
        self._queue.append(("patch", table, row, None, filters))
        self._wake()

    def _wake(self) -> None:
        """有事件循环就起 worker；没有（测试/离线脚本）就静静缓冲。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._drain())

    # ---------------- 消费 ----------------

    async def _drain(self) -> None:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
                while self._queue:
                    op, table, row, on_conflict, filters = self._queue.popleft()
                    try:
                        if op == "write":
                            await self._post(client, table, row, on_conflict)
                        else:
                            await self._patch(client, table, filters, row)
                    except Exception as e:                       # noqa: BLE001
                        log.warning("persist %s %s failed: %s", op, table, e)
        except Exception as e:                                   # noqa: BLE001
            log.warning("persistence worker crashed: %s", e)

    async def _post(self, client: httpx.AsyncClient, table: str,
                    row: dict, on_conflict: str | None) -> None:
        url = f"{self.url}/rest/v1/{table}"
        headers = {"Prefer": "return=minimal"}
        if on_conflict:
            url += f"?on_conflict={on_conflict}"
            headers["Prefer"] = "return=minimal,resolution=merge-duplicates"
        r = await client.post(url, json=row, headers=headers)
        if r.status_code >= 300:
            log.warning("persist %s -> %s %s", table, r.status_code, r.text[:200])

    async def _patch(self, client: httpx.AsyncClient, table: str,
                     filters: str, row: dict) -> None:
        r = await client.patch(f"{self.url}/rest/v1/{table}?{filters}", json=row,
                               headers={"Prefer": "return=minimal"})
        if r.status_code >= 300:
            log.warning("persist patch %s -> %s %s", table, r.status_code, r.text[:200])

    async def flush(self) -> None:
        """关服前把剩余队列写完（FastAPI shutdown 调用）。"""
        if not self.enabled:
            return
        if self._worker and not self._worker.done():
            await self._worker
        if self._queue:
            await self._drain()
        if self._dropped:
            log.warning("persistence 丢弃了 %d 行（队列满）", self._dropped)


persistence = Persistence()
