"""Whisper транскрипция-провайдер — локальная модель OpenAI Whisper."""

import asyncio
import logging

from app.config import WHISPER_MODEL
from app.services.providers.base import TranscriptionProvider

logger = logging.getLogger(__name__)

# Lazy singleton — модель загружается при первом использовании
_whisper_model = None
_model_lock = asyncio.Lock()


async def _get_whisper_model():
    """Загружает модель Whisper (lazy singleton)."""
    global _whisper_model

    async with _model_lock:
        if _whisper_model is not None:
            return _whisper_model

        logger.info(f"Загрузка Whisper модели '{WHISPER_MODEL}'...")

        # Whisper — синхронная библиотека, запускаем в thread pool
        def _load():
            import whisper
            return whisper.load_model(WHISPER_MODEL)

        _whisper_model = await asyncio.get_event_loop().run_in_executor(None, _load)
        logger.info(f"Whisper модель '{WHISPER_MODEL}' загружена")
        return _whisper_model


class WhisperProvider(TranscriptionProvider):
    name = "whisper"

    async def transcribe(self, file_path: str) -> dict:
        model = await _get_whisper_model()

        logger.info(f"Whisper: транскрибирую {file_path}...")

        # Whisper — CPU/GPU-intensive, запускаем в thread pool
        def _run_transcription():
            result = model.transcribe(
                file_path,
                language="ru",
                verbose=False,
            )
            return result

        result = await asyncio.get_event_loop().run_in_executor(None, _run_transcription)

        # Преобразуем формат Whisper → наш формат
        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "start": seg["start"],
                "end": seg["end"],
                "speaker": "",  # Whisper не определяет спикеров
                "text": seg["text"].strip(),
            })

        full_text = result.get("text", "").strip()

        logger.info(f"Whisper: готово — {len(full_text)} символов, {len(segments)} сегментов")

        return {
            "full_text": full_text,
            "segments": segments,
        }

    async def health_check(self) -> bool:
        try:
            import whisper
            return True
        except ImportError:
            return False
