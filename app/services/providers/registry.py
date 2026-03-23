"""Реестр провайдеров — возвращает активный провайдер по конфигу."""

import logging

from app.services.providers.base import LLMProvider, TranscriptionProvider

logger = logging.getLogger(__name__)

_llm_instance: LLMProvider | None = None
_transcription_instance: TranscriptionProvider | None = None


def _make_ollama() -> LLMProvider:
    from app.services.providers.ollama_provider import OllamaProvider
    return OllamaProvider()


def _make_claude() -> LLMProvider:
    from app.services.providers.claude_provider import ClaudeProvider
    return ClaudeProvider()


def get_llm_provider() -> LLMProvider:
    """Возвращает текущий LLM-провайдер (singleton, пересоздаётся при смене)."""
    global _llm_instance

    from app.config import LLM_PROVIDER

    if _llm_instance and _llm_instance.name == LLM_PROVIDER:
        return _llm_instance

    if LLM_PROVIDER == "ollama":
        _llm_instance = _make_ollama()
    elif LLM_PROVIDER == "claude":
        _llm_instance = _make_claude()
    elif LLM_PROVIDER == "auto":
        _llm_instance = _make_ollama()  # default для auto — singleton не важен
    else:
        raise ValueError(f"Неизвестный LLM провайдер: {LLM_PROVIDER}")

    logger.info(f"LLM провайдер: {LLM_PROVIDER}")
    return _llm_instance


def get_provider_for_text(text_length: int) -> LLMProvider:
    """Умная маршрутизация: выбирает провайдер по длине текста.

    - auto: короткий текст → Ollama, длинный → Claude (если есть ключ)
    - ollama/claude: всегда один провайдер
    """
    from app.config import LLM_PROVIDER, AUTO_ROUTING_THRESHOLD, ANTHROPIC_API_KEY

    if LLM_PROVIDER == "ollama":
        provider = _make_ollama()
    elif LLM_PROVIDER == "claude":
        provider = _make_claude()
    elif LLM_PROVIDER == "auto":
        if text_length < AUTO_ROUTING_THRESHOLD:
            provider = _make_ollama()
        elif ANTHROPIC_API_KEY:
            provider = _make_claude()
        else:
            provider = _make_ollama()  # fallback если нет API-ключа
    else:
        provider = _make_ollama()

    logger.info(f"Auto-routing: {text_length} символов → {provider.name}")
    return provider


def get_transcription_provider() -> TranscriptionProvider:
    """Возвращает текущий транскрипция-провайдер (singleton, пересоздаётся при смене)."""
    global _transcription_instance

    from app.config import TRANSCRIPTION_PROVIDER

    if _transcription_instance and _transcription_instance.name == TRANSCRIPTION_PROVIDER:
        return _transcription_instance

    if TRANSCRIPTION_PROVIDER == "whisper":
        from app.services.providers.whisper_provider import WhisperProvider
        _transcription_instance = WhisperProvider()
    elif TRANSCRIPTION_PROVIDER == "bukvitsa":
        from app.services.providers.bukvitsa_provider import BukvitsaProvider
        _transcription_instance = BukvitsaProvider()
    else:
        raise ValueError(f"Неизвестный транскрипция провайдер: {TRANSCRIPTION_PROVIDER}")

    logger.info(f"Транскрипция провайдер: {_transcription_instance.name}")
    return _transcription_instance


def reset_llm_provider():
    """Сбрасывает кэш LLM-провайдера (для смены в настройках)."""
    global _llm_instance
    _llm_instance = None


def reset_transcription_provider():
    """Сбрасывает кэш транскрипция-провайдера (для смены в настройках)."""
    global _transcription_instance
    _transcription_instance = None
