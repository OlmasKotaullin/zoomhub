"""Генерация умного конспекта через LLM-провайдер (Claude / Ollama)."""

import json
import logging

from app.services.providers.registry import get_provider_for_text, make_provider_by_name

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — ассистент для обработки записей рабочих встреч.
Тебе дан транскрипт РЕАЛЬНОЙ встречи. Сгенерируй структурированный конспект.

ВАЖНО: Используй ТОЛЬКО информацию из транскрипта. Не выдумывай.

Ответ СТРОГО в формате JSON:
{
  "tldr": "Краткое резюме встречи в 3-5 предложениях. Укажи участников, тему и ключевые решения.",
  "tasks": [
    {"task": "Конкретная задача из встречи", "assignee": "Имя ответственного из транскрипта", "deadline": "Срок если упомянут, иначе пустая строка"}
  ],
  "topics": [
    {"topic": "Название обсуждённой темы", "details": "Что именно обсуждали и к чему пришли"}
  ],
  "insights": [
    {"insight": "Важная мысль или идея, озвученная на встрече"}
  ]
}

Если задач, тем или инсайтов нет — верни пустые массивы.
Пиши на русском языке."""

# Лимиты контекста для провайдеров (в символах)
CONTEXT_LIMITS = {
    "gemini": 900000,   # 1M токенов — любая встреча поместится
    "groq": 100000,     # 128K токенов (Llama 3.3 70B) — до 2ч встречи
    "claude": 100000,
    "deepseek": 100000,  # 128K токенов — до 2ч встречи
    "gigachat": 12000,   # ~8K токенов — слабая модель, мало контекста
    "ollama": 12000,     # ~16K токенов при num_ctx=16384
}

# GigaChat слишком слабый для саммари — всегда используем Gemini если есть ключ
SUMMARIZER_PROVIDER_OVERRIDE = "gemini"  # принудительно для суммаризации


async def generate_summary(transcript_text: str, provider_name: str | None = None) -> dict:
    """Генерирует конспект из текста транскрипта.

    Args:
        transcript_text: текст транскрипта
        provider_name: имя провайдера (groq/gemini/claude/ollama) или None для автовыбора

    Returns:
        {"tldr": str, "tasks": list, "topics": list, "insights": list, "raw_response": str}
    """
    from app.config import GOOGLE_AI_API_KEY, ANTHROPIC_API_KEY, DEEPSEEK_API_KEY

    if provider_name:
        providers = [make_provider_by_name(provider_name)]
    else:
        # Для суммаризации используем лучший доступный провайдер (не GigaChat)
        if GOOGLE_AI_API_KEY:
            providers = [make_provider_by_name("gemini")]
        elif DEEPSEEK_API_KEY:
            providers = [make_provider_by_name("deepseek")]
        elif ANTHROPIC_API_KEY:
            providers = [make_provider_by_name("claude")]
        else:
            provider = get_provider_for_text(len(transcript_text))
            providers = [provider]

    # Fallback chain
    if not provider_name:
        primary = providers[0].name
        if primary != "gemini" and GOOGLE_AI_API_KEY:
            providers.append(make_provider_by_name("gemini"))
        if primary != "deepseek" and DEEPSEEK_API_KEY:
            providers.append(make_provider_by_name("deepseek"))
        if primary != "claude" and ANTHROPIC_API_KEY:
            providers.append(make_provider_by_name("claude"))

    for provider in providers:
        max_chars = CONTEXT_LIMITS.get(provider.name, 20000)
        text = transcript_text[:max_chars]

        if len(transcript_text) > max_chars:
            logger.info(f"Транскрипт обрезан: {len(transcript_text)} → {max_chars} символов для {provider.name}")

        try:
            raw_response = await provider.generate(
                messages=[
                    {"role": "user", "content": f"Вот транскрипт встречи:\n\n{text}"}
                ],
                system=SYSTEM_PROMPT,
                json_mode=True,
                max_tokens=8192,
            )
            result = _parse_summary(raw_response)
            if result.get("tldr"):
                logger.info(f"Саммари сгенерировано через {provider.name}")
                return result
            logger.warning(f"Пустой ответ от {provider.name}, пробую следующий...")
        except Exception as e:
            logger.warning(f"LLM ({provider.name}) ошибка: {e}, пробую следующий...")

    logger.error("Все LLM провайдеры не смогли сгенерировать саммари")
    return empty_summary()


def _parse_summary(raw_response: str) -> dict:
    """Парсит JSON-ответ LLM."""
    import re

    try:
        text = raw_response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]

        data = json.loads(text)
        return {
            "tldr": data.get("tldr", ""),
            "tasks": data.get("tasks", []),
            "topics": data.get("topics", []),
            "insights": data.get("insights", []),
            "raw_response": raw_response,
        }
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Не удалось распарсить JSON: {e}")
        # Попытка извлечь tldr из обрезанного JSON
        tldr_match = re.search(r'"tldr"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_response)
        if tldr_match:
            return {
                "tldr": tldr_match.group(1),
                "tasks": [],
                "topics": [],
                "insights": [],
                "raw_response": raw_response,
            }
        return {
            "tldr": "",
            "tasks": [],
            "topics": [],
            "insights": [],
            "raw_response": raw_response,
        }


def empty_summary() -> dict:
    return {
        "tldr": "",
        "tasks": [],
        "topics": [],
        "insights": [],
        "raw_response": "",
    }
