"""Базовые интерфейсы провайдеров."""

from abc import ABC, abstractmethod
from typing import AsyncGenerator


class LLMProvider(ABC):
    """Абстрактный провайдер для LLM (Claude, Ollama и др.)."""

    name: str = "unknown"

    @abstractmethod
    async def generate(self, messages: list[dict], system: str = "",
                       json_mode: bool = False, max_tokens: int = 4096) -> str:
        """Генерирует ответ на основе сообщений.

        Args:
            messages: Список сообщений [{role, content}]
            system: Системный промпт
            json_mode: Требовать JSON-формат ответа
            max_tokens: Максимум токенов в ответе

        Returns:
            Текст ответа модели
        """
        ...

    async def generate_stream(self, messages: list[dict], system: str = "",
                              max_tokens: int = 4096) -> AsyncGenerator[str, None]:
        """Потоковая генерация — yield-ит текстовые чанки.

        Дефолт: вызывает generate() и отдаёт целиком.
        Провайдеры переопределяют для настоящего streaming.
        """
        result = await self.generate(messages, system=system, max_tokens=max_tokens)
        yield result

    @abstractmethod
    async def health_check(self) -> bool:
        """Проверяет доступность провайдера."""
        ...


class TranscriptionProvider(ABC):
    """Абстрактный провайдер для транскрипции аудио."""

    name: str = "unknown"

    @abstractmethod
    async def transcribe(self, file_path: str, user_id: int | None = None) -> dict:
        """Транскрибирует аудиофайл.

        Args:
            file_path: Путь к аудиофайлу
            user_id: ID пользователя для per-user сессии (Буквица)

        Returns:
            {"full_text": str, "segments": list[dict]}
            где segments = [{"start": float, "end": float, "speaker": str, "text": str}]
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Проверяет доступность провайдера."""
        ...
