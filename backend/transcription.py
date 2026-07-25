"""外部语音转写适配器。默认 OpenAI Audio Transcriptions API。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from functools import lru_cache

import httpx


class TranscriptionError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value.strip()
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, item = line.partition("=")
                if key.strip() == name:
                    return item.strip()
    return default


def configured() -> bool:
    return bool(_env("OPENAI_API_KEY"))


@lru_cache(maxsize=1)
def _local_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "未安装 faster-whisper，请执行 .venv312/bin/pip install -r requirements.txt"
        ) from exc
    # demo 优先低延迟；正式环境可设 RING_WHISPER_MODEL=small 提升中文准确率。
    model_name = _env("RING_WHISPER_MODEL", "tiny")
    return WhisperModel(model_name, device="cpu", compute_type="int8")


def _to_wav(source: Path) -> Path:
    """把 Ogg/Speex/WAV 或设备 raw 尝试转成 API 可接受的 WAV。"""
    fd, name = tempfile.mkstemp(prefix="ring-transcribe-", suffix=".wav")
    os.close(fd)
    target = Path(name)
    commands = [
        ["ffmpeg", "-y", "-i", str(source), "-ac", "1", "-ar", "16000", str(target)],
        ["ffmpeg", "-y", "-f", "speex", "-ar", "16000", "-ac", "1", "-i", str(source),
         str(target)],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and target.stat().st_size > 44:
            return target
    target.unlink(missing_ok=True)
    raise TranscriptionError("ffmpeg 无法识别戒指录音编码")


async def transcribe_file(source: Path) -> str:
    """转写一条音频；默认本地 faster-whisper，不上传音频。"""
    mode = _env("RING_TRANSCRIBER", "local").lower()
    if mode in {"local", "faster-whisper", "whisper"}:
        wav = await asyncio.to_thread(_to_wav, source)
        try:
            def run_local() -> str:
                model = _local_model()
                segments, _ = model.transcribe(
                    str(wav), language="zh", beam_size=5, vad_filter=True)
                return "".join(segment.text for segment in segments).strip()
            text = await asyncio.to_thread(run_local)
            if not text:
                raise TranscriptionError("本地转写返回空文本")
            return text
        finally:
            wav.unlink(missing_ok=True)
    if mode not in {"openai", "cloud"}:
        raise TranscriptionError(f"未知 RING_TRANSCRIBER={mode}")
    key = _env("OPENAI_API_KEY")
    if not key:
        raise TranscriptionError("未配置 OPENAI_API_KEY")
    model = _env("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
    wav = await asyncio.to_thread(_to_wav, source)
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            with wav.open("rb") as audio:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": ("ring.wav", audio, "audio/wav")},
                    data={"model": model, "language": "zh"},
                )
        if response.status_code >= 400:
            raise TranscriptionError(f"转写 API {response.status_code}: {response.text[:300]}")
        text = str(response.json().get("text", "")).strip()
        if not text:
            raise TranscriptionError("转写 API 返回空文本")
        return text
    finally:
        wav.unlink(missing_ok=True)
