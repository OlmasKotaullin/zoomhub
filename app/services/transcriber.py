"""Транскрибация аудио через модульный провайдер (Буквица / Whisper / ...).

Тонкая обёртка — вся логика в конкретных провайдерах:
  - app/services/providers/bukvitsa_provider.py
  - app/services/providers/whisper_provider.py
"""

import logging

from app.services.providers import get_transcription_provider

# Re-exports для обратной совместимости (используются в routers и тестах)
from app.services.providers.bukvitsa_provider import (
    parse_response,
    _extract_transcript_section,
    _parse_time,
    _get_client,
)

logger = logging.getLogger(__name__)


async def transcribe_file(file_path: str, user_id: int | None = None) -> dict:
    """Транскрибирует файл через активный провайдер.

    user_id: если передан, провайдер использует личную Telegram-сессию пользователя.

    Returns:
        {"full_text": str, "segments": list[dict]}
    """
    provider = get_transcription_provider()
    logger.info(f"Транскрипция через {provider.name}: {file_path}")
    return await provider.transcribe(file_path, user_id=user_id)
