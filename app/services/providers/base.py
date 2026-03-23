"""Базовые интерфейсы провайдеров."""

from abc import ABC, abstractmethod


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

    @abstractmethod
    async def health_check(self) -> bool:
        """Проверяет доступность провайдера."""
        ...


class TranscriptionProvider(ABC):
    """Абстрактный провайдер для транскрипции аудио."""

    name: str = "unknown"

    @abstractmethod
    async def transcribe(self, file_path: str) -> dict:
        """Транскрибирует аудиофайл.

        Args:
            file_path: Путь к аудиофайлу

        Returns:
            {"full_text": str, "segments": list[dict]}
            где segments = [{"start": float, "end": float, "speaker": str, "text": str}]
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Проверяет доступность провайдера."""
        ...
