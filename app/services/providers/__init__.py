"""Модульные провайдеры для LLM и транскрипции."""

from app.services.providers.base import LLMProvider, TranscriptionProvider
from app.services.providers.registry import get_llm_provider, get_transcription_provider

__all__ = [
    "LLMProvider",
    "TranscriptionProvider",
    "get_llm_provider",
    "get_transcription_provider",
]
