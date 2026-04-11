"""Реестр провайдеров — возвращает активный провайдер по конфигу."""

import logging

from app.services.providers.base import LLMProvider, TranscriptionProvider

logger = logging.getLogger(__name__)

_llm_instance: LLMProvider | None = None
_transcription_instance: TranscriptionProvider | None = None


def _make_ollama(api_key: str | None = None) -> LLMProvider:
    from app.services.providers.ollama_provider import OllamaProvider
    return OllamaProvider()


def _make_claude(api_key: str | None = None) -> LLMProvider:
    from app.services.providers.claude_provider import ClaudeProvider
    return ClaudeProvider(api_key=api_key)


def _make_gemini(api_key: str | None = None) -> LLMProvider:
    from app.services.providers.gemini_provider import GeminiProvider
    return GeminiProvider(api_key=api_key)


def _make_groq(api_key: str | None = None) -> LLMProvider:
    from app.services.providers.groq_provider import GroqProvider
    return GroqProvider(api_key=api_key)


def _make_gigachat(api_key: str | None = None) -> LLMProvider:
    from app.services.providers.gigachat_provider import GigaChatProvider
    return GigaChatProvider(auth_key=api_key)


def _make_deepseek(api_key: str | None = None) -> LLMProvider:
    from app.services.providers.deepseek_provider import DeepSeekProvider
    return DeepSeekProvider(api_key=api_key)


def _make_openrouter(api_key: str | None = None) -> LLMProvider:
    from app.services.providers.openrouter_provider import OpenRouterProvider
    return OpenRouterProvider(api_key=api_key)


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
    elif LLM_PROVIDER == "gemini":
        _llm_instance = _make_gemini()
    elif LLM_PROVIDER == "groq":
        _llm_instance = _make_groq()
    elif LLM_PROVIDER == "gigachat":
        _llm_instance = _make_gigachat()
    elif LLM_PROVIDER == "deepseek":
        _llm_instance = _make_deepseek()
    elif LLM_PROVIDER == "openrouter":
        _llm_instance = _make_openrouter()
    elif LLM_PROVIDER == "auto":
        _llm_instance = _make_gigachat()  # default для auto — GigaChat (бесплатный, работает из РФ)
    else:
        raise ValueError(f"Неизвестный LLM провайдер: {LLM_PROVIDER}")

    logger.info(f"LLM провайдер: {LLM_PROVIDER}")
    return _llm_instance


def get_provider_for_text(text_length: int) -> LLMProvider:
    """Умная маршрутизация: выбирает провайдер по длине текста.

    - auto: короткий текст → Ollama, длинный → Claude (если есть ключ)
    - ollama/claude: всегда один провайдер
    """
    from app.config import LLM_PROVIDER, AUTO_ROUTING_THRESHOLD, ANTHROPIC_API_KEY, GOOGLE_AI_API_KEY

    from app.config import GROQ_API_KEY

    if LLM_PROVIDER == "ollama":
        provider = _make_ollama()
    elif LLM_PROVIDER == "claude":
        provider = _make_claude()
    elif LLM_PROVIDER == "gemini":
        provider = _make_gemini()
    elif LLM_PROVIDER == "groq":
        provider = _make_groq()
    elif LLM_PROVIDER == "gigachat":
        provider = _make_gigachat()
    elif LLM_PROVIDER == "deepseek":
        provider = _make_deepseek()
    elif LLM_PROVIDER == "openrouter":
        provider = _make_openrouter()
    elif LLM_PROVIDER == "auto":
        from app.config import GIGACHAT_AUTH_KEY
        if GIGACHAT_AUTH_KEY:
            provider = _make_gigachat()
        elif GROQ_API_KEY:
            provider = _make_groq()
        elif GOOGLE_AI_API_KEY:
            provider = _make_gemini()
        elif text_length < AUTO_ROUTING_THRESHOLD:
            provider = _make_ollama()
        elif ANTHROPIC_API_KEY:
            provider = _make_claude()
        else:
            provider = _make_ollama()
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
    elif TRANSCRIPTION_PROVIDER == "openai_whisper":
        from app.services.providers.openai_whisper_provider import OpenAIWhisperProvider
        _transcription_instance = OpenAIWhisperProvider()
    elif TRANSCRIPTION_PROVIDER == "runpod_whisper":
        from app.services.providers.runpod_provider import RunPodWhisperProvider
        _transcription_instance = RunPodWhisperProvider()
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


def make_provider_by_name(name: str, user_keys: dict | None = None) -> LLMProvider:
    """Создаёт LLM-провайдер по имени (без кэширования).

    user_keys: {"groq": "gsk_...", "gemini": "AIza...", "anthropic": "sk-ant-...", "openai": "sk-..."}
    """
    key = (user_keys or {}).get(name) or (user_keys or {}).get(
        {"claude": "anthropic"}.get(name, name)
    )
    factories = {
        "groq": _make_groq,
        "gemini": _make_gemini,
        "claude": _make_claude,
        "ollama": _make_ollama,
        "gigachat": _make_gigachat,
        "deepseek": _make_deepseek,
        "openrouter": _make_openrouter,
    }
    factory = factories.get(name)
    if not factory:
        raise ValueError(f"Неизвестный провайдер: {name}")
    return factory(api_key=key)


def get_chat_provider_chain() -> list[LLMProvider]:
    """Возвращает цепочку провайдеров для AI-чата (основной + fallback).

    Приоритет: качество + контекст + бесплатность.
    Gemini (900K) → Groq (128K) → Claude (200K) → GigaChat (12K fallback)
    """
    from app.config import (
        GOOGLE_AI_API_KEY, GROQ_API_KEY, ANTHROPIC_API_KEY,
        GIGACHAT_AUTH_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY,
    )

    chain = []
    if GOOGLE_AI_API_KEY:
        chain.append(_make_gemini())
    if GROQ_API_KEY:
        chain.append(_make_groq())
    if OPENROUTER_API_KEY:
        chain.append(_make_openrouter())
    if DEEPSEEK_API_KEY:
        chain.append(_make_deepseek())
    if ANTHROPIC_API_KEY:
        chain.append(_make_claude())
    if GIGACHAT_AUTH_KEY:
        chain.append(_make_gigachat())

    if not chain:
        chain = [_make_ollama()]

    return chain


def get_user_keys(user) -> dict:
    """Извлекает пользовательские API-ключи из объекта User."""
    if not user:
        return {}
    keys = {}
    if getattr(user, "user_groq_api_key", None):
        keys["groq"] = user.user_groq_api_key
    if getattr(user, "user_gemini_api_key", None):
        keys["gemini"] = user.user_gemini_api_key
    if getattr(user, "user_anthropic_api_key", None):
        keys["anthropic"] = user.user_anthropic_api_key
        keys["claude"] = user.user_anthropic_api_key
    if getattr(user, "user_openai_api_key", None):
        keys["openai"] = user.user_openai_api_key
    if getattr(user, "user_gigachat_auth_key", None):
        keys["gigachat"] = user.user_gigachat_auth_key
    if getattr(user, "user_deepseek_api_key", None):
        keys["deepseek"] = user.user_deepseek_api_key
    if getattr(user, "user_openrouter_api_key", None):
        keys["openrouter"] = user.user_openrouter_api_key
    return keys


def get_available_providers() -> list[dict]:
    """Возвращает список провайдеров с информацией о доступности."""
    from app.config import GROQ_API_KEY, GOOGLE_AI_API_KEY, ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY

    return [
        {"name": "openrouter", "label": "OpenRouter (Llama 3.3 70B, бесплатно)", "available": bool(OPENROUTER_API_KEY)},
        {"name": "deepseek", "label": "DeepSeek V3 (128K)", "available": bool(DEEPSEEK_API_KEY)},
        {"name": "groq", "label": "Groq (Llama 3.3 70B)", "available": bool(GROQ_API_KEY)},
        {"name": "gemini", "label": "Gemini Flash", "available": bool(GOOGLE_AI_API_KEY)},
        {"name": "claude", "label": "Claude Sonnet", "available": bool(ANTHROPIC_API_KEY)},
        {"name": "ollama", "label": "Ollama (локальный)", "available": False},  # нет на сервере
    ]
