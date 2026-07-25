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
import re
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("redsignal.persistence")

_MAX_QUEUE = 10_000          # 满了丢最旧的，绝不无限增长
_TIMEOUT = 5.0
_MAX_ATTEMPTS = 3            # 瞬时网络故障重试次数
_RETRY_BACKOFF = 0.5         # 秒，按次数线性递增
# PostgREST 在列不存在时返回 PGRST204，message 形如
#   Could not find the 'auth_user_id' column of 'user_event_profiles' in the schema cache
_UNKNOWN_COL_RE = re.compile(r"Could not find the '([^']+)' column")


def _unknown_column(resp) -> Optional[str]:
    """从 PostgREST 的报错里认出「这一列不存在」，返回列名。"""
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("code") != "PGRST204":
        return None
    m = _UNKNOWN_COL_RE.search(body.get("message") or "")
    return m.group(1) if m else None


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
        # (表, 列) —— 线上 schema 里不存在的列，写入时自动摘掉
        self._missing_columns: set[tuple[str, str]] = set()
        if not self.enabled:
            log.info("persistence disabled (缺 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")

    # ---------------- 入队（同步，永不阻塞调用方） ----------------

    def write(self, table: str, row: dict, on_conflict: str | None = None) -> None:
        """插入或 upsert 一行。on_conflict 给出冲突列则走 merge-duplicates。"""
        if not self.enabled:
            return
        if len(self._queue) == self._queue.maxlen:
            self._dropped += 1
        self._queue.append(("write", table, row, on_conflict, None, 0))
        self._wake()

    def patch(self, table: str, filters: str, row: dict) -> None:
        """按 PostgREST 过滤串更新，例如 filters='user_id=eq.x&event_id=eq.y'。"""
        if not self.enabled:
            return
        self._queue.append(("patch", table, row, None, filters, 0))
        self._wake()

    def _wake(self) -> None:
        """有事件循环就起 worker；没有（测试/离线脚本）就静静缓冲。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._drain())
            self._worker.add_done_callback(self._rewake_if_pending)

    def _rewake_if_pending(self, _task) -> None:
        """补掉丢唤醒：worker 判空正要退出时若来了新行，_wake 会看到
        worker 尚未 done 而不起新的，那一行就搁在队列里，得等下次写入
        才被捎带出去——空闲前的最后一次写入因此可能迟迟不落库。
        worker 结束后再看一眼队列，有货就重新拉起。"""
        if self._queue:
            self._wake()

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
                    op, table, row, on_conflict, filters, attempts = self._queue.popleft()
                    try:
                        if op == "write":
                            await self._post(client, table, row, on_conflict)
                        else:
                            await self._patch(client, table, filters, row)
                    except Exception as e:                       # noqa: BLE001
                        # 瞬时网络故障（实测见过 ConnectTimeout）以前是直接丢行的——
                        # 对"值得留档"的写回层来说那就是静默数据丢失。放回队首重试，
                        # 队首而非队尾是为了保住 FK 顺序（父行必须先落地）。
                        if attempts + 1 < _MAX_ATTEMPTS:
                            self._queue.appendleft(
                                (op, table, row, on_conflict, filters, attempts + 1))
                            log.warning("persist %s %s 第 %d 次失败(%s)，%.1fs 后重试",
                                        op, table, attempts + 1, type(e).__name__,
                                        _RETRY_BACKOFF * (attempts + 1))
                            await asyncio.sleep(_RETRY_BACKOFF * (attempts + 1))
                        else:
                            self._dropped += 1
                            log.error("persist %s %s 重试 %d 次仍失败，丢弃该行: %r",
                                      op, table, _MAX_ATTEMPTS, e)
        except Exception as e:                                   # noqa: BLE001
            log.warning("persistence worker crashed: %r", e)

    async def _post(self, client: httpx.AsyncClient, table: str,
                    row: dict, on_conflict: str | None) -> None:
        url = f"{self.url}/rest/v1/{table}"
        headers = {"Prefer": "return=minimal"}
        if on_conflict:
            url += f"?on_conflict={on_conflict}"
            headers["Prefer"] = "return=minimal,resolution=merge-duplicates"
        row = {k: v for k, v in row.items() if (table, k) not in self._missing_columns}
        r = await client.post(url, json=row, headers=headers)

        # 代码比线上 schema 新时（迁移还没跑），PostgREST 报 PGRST204 指出未知列。
        # 与其让整行写入失败、数据静默丢掉，不如摘掉那一列重发一次，
        # 并记住它——迁移跑完重启进程即恢复完整写入。
        if r.status_code >= 300:
            col = _unknown_column(r)
            if col and (table, col) in self._missing_columns:
                pass                       # 已经摘过还失败，说明是别的问题
            elif col:
                self._missing_columns.add((table, col))
                log.warning("表 %s 缺列 %s（迁移未跑？），本次起跳过该列继续写入。"
                            "补列：docs/supabase_schema.sql 末尾的 alter table 语句",
                            table, col)
                return await self._post(client, table, row, on_conflict)
            log.warning("persist %s -> %s %s", table, r.status_code, r.text[:200])

    async def _patch(self, client: httpx.AsyncClient, table: str,
                     filters: str, row: dict) -> None:
        r = await client.patch(f"{self.url}/rest/v1/{table}?{filters}", json=row,
                               headers={"Prefer": "return=minimal"})
        if r.status_code >= 300:
            log.warning("persist patch %s -> %s %s", table, r.status_code, r.text[:200])

    def read(self, table: str, filters: str = "", limit: int = 500) -> list[dict]:
        """同步读一次（只在冷路径用：进程重启后恢复聊天记录）。

        写入是异步队列，因为 sighting 是每秒数十次的热路径；
        读不一样——一次会话只读一次，直接同步请求最简单，
        也避免了"读到一半队列还没落库"的时序问题。
        """
        if not self.enabled:
            return []
        try:
            r = httpx.get(f"{self.url}/rest/v1/{table}?{filters}&limit={limit}",
                          timeout=_TIMEOUT,
                          headers={"apikey": self.key,
                                   "Authorization": f"Bearer {self.key}"})
            if r.status_code >= 300:
                log.warning("read %s -> %s %s", table, r.status_code, r.text[:150])
                return []
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:                                # noqa: BLE001
            log.warning("read %s failed: %r", table, e)
            return []

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
