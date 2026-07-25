"""Zilo 戒指录音自动提取状态机。

设备不会因为 0503 自动把音频推出来：必须先拿 0504 元信息，再用 0506
逐偏移请求 0505。这个模块只负责协议状态机和本地 raw 保存，调用方提供
异步 send_frame 回调，因此可复用于 WebSocket 和命令行探针。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import zilo_protocol as zp

log = logging.getLogger("redsignal.ring_audio")
SendFrame = Callable[[bytes], Awaitable[None]]
Notify = Callable[[dict], Awaitable[None]]
Transcript = Callable[[Path, dict], Awaitable[None]]


@dataclass
class _Transfer:
    file_index: int
    metadata: dict = field(default_factory=dict)
    data: bytearray = field(default_factory=bytearray)
    expected_offset: int = 0
    waiting_done: bool = False


class RingAudioSession:
    """单个用户/单个戒指连接的录音拉取会话。"""

    def __init__(self, user_id: str, send_frame: SendFrame,
                 notify: Optional[Notify] = None,
                 on_transcript: Optional[Transcript] = None,
                 output_dir: Optional[Path] = None) -> None:
        self.user_id = user_id
        self.send_frame = send_frame
        self.notify = notify
        self.on_transcript = on_transcript
        self.output_dir = output_dir or Path("data/ring_audio")
        self.known_count: Optional[int] = None
        self.pending: deque[int] = deque()
        self.queued: set[int] = set()
        self.transfer: Optional[_Transfer] = None
        self._pump_lock = asyncio.Lock()

    async def emit(self, event: dict) -> None:
        if self.notify:
            await self.notify({"type": "ring_audio", **event})

    async def request_list(self) -> None:
        await self.send_frame(zp.build_frame(zp.CMD_AUDIO_LIST))

    async def handle_frame(self, frame: zp.ZiloFrame) -> None:
        if frame.cmd == zp.CMD_AUDIO_LIST_RESP:
            info = zp.parse_audio_list_body(frame.body)
            if info["errorCode"] not in (None, 0):
                await self.emit({"stage": "error", "errorCode": info["errorCode"]})
                return
            count = int(info["fileCount"])
            if self.known_count is None:
                self.known_count = count
                await self.emit({"stage": "ready", "fileCount": count})
            elif count > self.known_count:
                for index in range(self.known_count, count):
                    self.enqueue(index)
                self.known_count = count
                await self.emit({"stage": "new_recordings", "fileCount": count})
            await self.pump()
            return

        if frame.cmd == zp.CMD_AUDIO_CREATED:
            if len(frame.body) >= 6:
                info = zp.parse_audio_metadata_body(frame.body)
                self.enqueue(int(info["fileIndex"]))
            elif len(frame.body) >= 2:
                index = int.from_bytes(frame.body[2:], "big")
                self.enqueue(index)
            await self.pump()
            return

        transfer = self.transfer
        if not transfer:
            return

        if frame.cmd == zp.CMD_AUDIO_METADATA:
            info = zp.parse_audio_metadata_body(frame.body)
            if info["fileIndex"] != transfer.file_index:
                return
            if info["errorCode"] not in (None, 0):
                await self.fail(info["errorCode"])
                return
            transfer.metadata = info
            await self.send_frame(zp.build_audio_next(transfer.file_index, 0))
            await self.emit({"stage": "downloading", "fileIndex": transfer.file_index,
                             "receivedBytes": 0, "totalBytes": info["dataSize"]})
            return

        if frame.cmd == zp.CMD_AUDIO_DATA:
            info = zp.parse_audio_data_body(frame.body)
            if info["fileIndex"] != transfer.file_index:
                return
            if info["errorCode"] not in (None, 0):
                await self.fail(info["errorCode"])
                return
            if info["offset"] != transfer.expected_offset:
                await self.send_frame(zp.build_audio_next(
                    transfer.file_index, transfer.expected_offset))
                return
            transfer.data.extend(info["data"])
            transfer.expected_offset += len(info["data"])
            total = int(transfer.metadata.get("dataSize") or 0)
            done = bool(info["isEnd"] or (total and len(transfer.data) >= total))
            await self.emit({"stage": "downloading", "fileIndex": transfer.file_index,
                             "receivedBytes": len(transfer.data), "totalBytes": total})
            if done:
                transfer.waiting_done = True
                await self.send_frame(zp.build_audio_extract_finish(transfer.file_index))
            else:
                await self.send_frame(zp.build_audio_next(
                    transfer.file_index, transfer.expected_offset))
            return

        if frame.cmd == zp.CMD_AUDIO_TRANSFER_DONE and transfer.waiting_done:
            await self.finish_transfer()

    def enqueue(self, file_index: int) -> None:
        if file_index < 0 or file_index in self.queued:
            return
        if self.transfer and self.transfer.file_index == file_index:
            return
        self.pending.append(file_index)
        self.queued.add(file_index)

    async def pump(self) -> None:
        async with self._pump_lock:
            if self.transfer or not self.pending:
                return
            index = self.pending.popleft()
            self.transfer = _Transfer(file_index=index)
            await self.send_frame(zp.build_audio_extract_start(index))
            await self.emit({"stage": "metadata", "fileIndex": index})

    async def finish_transfer(self) -> None:
        transfer = self.transfer
        if not transfer:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.user_id)
        stem = f"{safe_user}_{transfer.file_index}_{transfer.metadata.get('recordTime', 0)}"
        raw_path = self.output_dir / f"{stem}.bin"
        meta_path = self.output_dir / f"{stem}.json"
        raw_path.write_bytes(transfer.data)
        meta_path.write_text(json.dumps({
            "userId": self.user_id,
            "fileIndex": transfer.file_index,
            "metadata": transfer.metadata,
            "size": len(transfer.data),
            "rawPath": str(raw_path),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        await self.emit({"stage": "completed", "fileIndex": transfer.file_index,
                         "size": len(transfer.data), "rawPath": str(raw_path),
                         "metadata": transfer.metadata})
        if self.on_transcript:
            try:
                await self.on_transcript(raw_path, transfer.metadata)
            except Exception as exc:  # 转写失败不能影响后续录音拉取
                log.warning("ring audio transcription failed user=%s: %s", self.user_id, exc)
                await self.emit({"stage": "transcription_error", "fileIndex": transfer.file_index,
                                 "error": str(exc)})
        self.queued.discard(transfer.file_index)
        self.transfer = None
        await self.pump()

    async def fail(self, error_code: object) -> None:
        index = self.transfer.file_index if self.transfer else None
        await self.emit({"stage": "error", "fileIndex": index, "errorCode": error_code})
        if self.transfer:
            self.queued.discard(self.transfer.file_index)
        self.transfer = None
        await self.pump()
