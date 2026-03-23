"""OpenAI Whisper API — облачная транскрипция."""

import logging
import asyncio
from pathlib import Path

from app.services.providers.base import TranscriptionProvider

logger = logging.getLogger(__name__)


class OpenAIWhisperProvider(TranscriptionProvider):
    name = "openai_whisper"

    def __init__(self):
        from app.config import OPENAI_API_KEY
        self._api_key = OPENAI_API_KEY

    async def transcribe(self, file_path: str) -> dict:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self._api_key)

        file_size = Path(file_path).stat().st_size
        max_size = 25 * 1024 * 1024  # 25 MB OpenAI limit

        actual_path = file_path
        compressed = False

        # Compress if too large
        if file_size > max_size:
            logger.info(f"Файл {file_size / 1024 / 1024:.1f} МБ > 25 МБ — сжимаю...")
            actual_path = file_path + ".compressed.mp3"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", file_path,
                "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
                actual_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            compressed = True
            logger.info(f"Сжато до {Path(actual_path).stat().st_size / 1024 / 1024:.1f} МБ")

        try:
            with open(actual_path, "rb") as f:
                response = await client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language="ru",
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )

            segments = []
            for seg in getattr(response, "segments", []) or []:
                segments.append({
                    "start": seg.get("start", 0) if isinstance(seg, dict) else getattr(seg, "start", 0),
                    "end": seg.get("end", 0) if isinstance(seg, dict) else getattr(seg, "end", 0),
                    "speaker": "",
                    "text": seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", ""),
                })

            full_text = response.text if hasattr(response, "text") else str(response)

            logger.info(f"OpenAI Whisper: {len(segments)} сегментов, {len(full_text)} символов")
            return {"full_text": full_text, "segments": segments}

        finally:
            if compressed:
                Path(actual_path).unlink(missing_ok=True)

    async def health_check(self) -> bool:
        return bool(self._api_key)
