"""Транскрибация аудио — smart routing: Groq (быстро, бесплатно) → RunPod (fallback).

Тонкая обёртка — вся логика в конкретных провайдерах:
  - app/services/providers/bukvitsa_provider.py
  - app/services/providers/whisper_provider.py
  - app/services/providers/runpod_provider.py
"""

import logging
from pathlib import Path

from app.services.providers import get_transcription_provider

# Re-exports для обратной совместимости (используются в routers и тестах)
from app.services.providers.bukvitsa_provider import (
    parse_response,
    _extract_transcript_section,
    _parse_time,
    _get_client,
)

logger = logging.getLogger(__name__)

GROQ_MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB — Groq Whisper API limit


async def _transcribe_via_groq(file_path: str) -> dict | None:
    """Try transcription via Groq Whisper API. Returns None on failure."""
    from app.config import GROQ_API_KEY
    if not GROQ_API_KEY:
        return None

    try:
        import httpx
        file_size = Path(file_path).stat().st_size
        if file_size > GROQ_MAX_FILE_SIZE:
            logger.info(f"File too large for Groq ({file_size / 1024 / 1024:.1f} MB > 25 MB)")
            return None

        logger.info(f"Транскрипция через Groq Whisper: {file_size / 1024 / 1024:.1f} MB")

        async with httpx.AsyncClient(timeout=300) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": (Path(file_path).name, f, "audio/ogg")},
                    data={
                        "model": "whisper-large-v3",
                        "language": "ru",
                        "response_format": "verbose_json",
                    },
                )

            if resp.status_code != 200:
                logger.warning(f"Groq Whisper error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            full_text = data.get("text", "")
            segments = []
            for seg in data.get("segments", []):
                segments.append({
                    "start": seg.get("start", 0),
                    "end": seg.get("end", 0),
                    "text": seg.get("text", ""),
                })

            duration = data.get("duration", 0)
            logger.info(f"Groq Whisper done: {len(segments)} segments, {len(full_text)} chars, {duration:.0f}s")
            result = {"full_text": full_text, "segments": segments}
            if duration:
                result["duration_seconds"] = int(duration)
            return result

    except Exception as e:
        logger.warning(f"Groq Whisper failed: {e}")
        return None


async def transcribe_file(file_path: str, user_id: int | None = None) -> dict:
    """Транскрибирует файл: Groq (≤25MB, быстро) → configured provider (fallback).

    user_id: если передан, провайдер использует личную Telegram-сессию пользователя.

    Returns:
        {"full_text": str, "segments": list[dict]}
    """
    # Try Groq first (fast, free, ≤25 MB)
    result = await _transcribe_via_groq(file_path)
    if result and result.get("full_text"):
        return result

    # Fallback to configured provider (RunPod/Bukvitsa/etc)
    provider = get_transcription_provider()
    logger.info(f"Транскрипция через {provider.name}: {file_path}")
    return await provider.transcribe(file_path, user_id=user_id)
