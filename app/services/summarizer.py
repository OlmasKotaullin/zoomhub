"""Генерация умного конспекта через LLM-провайдер (Claude / Ollama)."""

import json
import logging

from app.services.providers.registry import get_provider_for_text

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
    "claude": 100000,
    "ollama": 12000,  # ~16K токенов при num_ctx=16384, минус системный промпт и JSON формат
}


async def generate_summary(transcript_text: str) -> dict:
    """Генерирует конспект из текста транскрипта.

    Returns:
        {"tldr": str, "tasks": list, "topics": list, "insights": list, "raw_response": str}
    """
    provider = get_provider_for_text(len(transcript_text))
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
            max_tokens=4096,
        )
        return _parse_summary(raw_response)

    except Exception as e:
        logger.error(f"LLM ({provider.name}) ошибка: {e}")
        return empty_summary()


def _parse_summary(raw_response: str) -> dict:
    """Парсит JSON-ответ LLM."""
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
        return {
            "tldr": raw_response[:500],
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
