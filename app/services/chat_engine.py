"""AI-чат по встречам через LLM-провайдер (Claude / Ollama)."""

import logging

from app.models import Meeting, Folder, ChatMessage
from app.services.providers.registry import get_provider_for_text

logger = logging.getLogger(__name__)

MAX_HISTORY = 20  # последних сообщений в контексте

MEETING_SYSTEM = """Ты — AI-ассистент для анализа рабочих встреч. Тебе предоставлен РЕАЛЬНЫЙ транскрипт и конспект конкретной встречи.

КРИТИЧЕСКИ ВАЖНО:
- Отвечай ТОЛЬКО на основе предоставленного контекста встречи
- НЕ выдумывай информацию, которой нет в транскрипте
- Цитируй конкретные фразы участников когда это уместно
- Называй реальные имена участников из транскрипта

Формат:
- Используй markdown: заголовки, списки, жирный текст
- Задачи оформляй как checklist (- [ ] задача → ответственный)
- Пиши на русском, кратко и по делу"""

FOLDER_SYSTEM = """Ты — AI-ассистент для анализа серии рабочих встреч. У тебя есть конспекты всех встреч из данной категории.

Правила:
- Сравнивай встречи между собой, находи паттерны и прогресс
- Используй markdown для структуры ответов
- Отслеживай выполнение задач между встречами
- Пиши на русском"""


def _build_meeting_context(meeting: Meeting) -> str:
    """Собирает контекст встречи для AI."""
    parts = [f"**Встреча:** {meeting.title}"]

    if meeting.date:
        parts.append(f"**Дата:** {meeting.date.strftime('%d.%m.%Y %H:%M')}")

    if meeting.summary:
        s = meeting.summary
        parts.append(f"**Резюме:** {s.tldr}")

        if s.tasks:
            tasks_str = "\n".join(
                f"- {t.get('task', '')}" + (f" → {t['assignee']}" if t.get('assignee') else "")
                for t in s.tasks
            )
            parts.append(f"**Задачи:**\n{tasks_str}")

        if s.topics:
            topics_str = "\n".join(
                f"- **{t.get('topic', '')}**: {t.get('details', '')}"
                for t in s.topics
            )
            parts.append(f"**Темы:**\n{topics_str}")

        if s.insights:
            insights_str = "\n".join(f"- {i.get('insight', '')}" for i in s.insights)
            parts.append(f"**Инсайты:**\n{insights_str}")

    if meeting.transcript:
        # Лимит транскрипта зависит от провайдера (Gemini = 900K, Claude = 100K, Ollama = 12K)
        from app.config import LLM_PROVIDER, GOOGLE_AI_API_KEY
        max_chars = 200000 if (LLM_PROVIDER in ("gemini", "auto") and GOOGLE_AI_API_KEY) else 20000
        text = meeting.transcript.full_text
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[...транскрипт обрезан, показано {max_chars} из {len(meeting.transcript.full_text)} символов]"
        parts.append(f"**Транскрипт:**\n{text}")

    return "\n\n".join(parts)


async def ask_about_meeting(meeting: Meeting, history: list[ChatMessage]) -> str:
    """Отвечает на вопрос по конкретной встрече."""
    context = _build_meeting_context(meeting)

    messages = [
        {"role": "user", "content": f"Контекст встречи:\n\n{context}"},
        {"role": "assistant", "content": "Я изучил материалы встречи. Задавайте вопросы."},
    ]

    recent = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history
    for msg in recent:
        messages.append({"role": msg.role.value, "content": msg.content})

    return await _call_llm(MEETING_SYSTEM, messages, context_length=len(context))


async def ask_about_folder(folder: Folder, history: list[ChatMessage]) -> str:
    """Отвечает на вопрос по всем встречам в папке."""
    parts = [f"**Категория:** {folder.name}\n**Встреч:** {len(folder.meetings)}"]

    for meeting in folder.meetings:
        meeting_info = f"\n---\n**{meeting.title}** ({meeting.date.strftime('%d.%m.%Y') if meeting.date else ''})"
        if meeting.summary:
            s = meeting.summary
            meeting_info += f"\n{s.tldr}"
            if s.tasks:
                meeting_info += "\nЗадачи: " + "; ".join(t.get('task', '') for t in s.tasks)
            if s.topics:
                meeting_info += "\nТемы: " + ", ".join(t.get('topic', '') for t in s.topics)
        elif meeting.transcript:
            meeting_info += f"\n{meeting.transcript.full_text[:3000]}"
        parts.append(meeting_info)

    context = "\n".join(parts)

    messages = [
        {"role": "user", "content": f"Контекст всех встреч:\n\n{context[:100000]}"},
        {"role": "assistant", "content": "Я изучил все встречи. Задавайте вопросы."},
    ]

    recent = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history
    for msg in recent:
        messages.append({"role": msg.role.value, "content": msg.content})

    return await _call_llm(FOLDER_SYSTEM, messages, context_length=len(context))


async def _call_llm(system: str, messages: list[dict], context_length: int = 0) -> str:
    """Вызывает LLM через auto-routing провайдер."""
    provider = get_provider_for_text(context_length)

    try:
        return await provider.generate(
            messages=messages,
            system=system,
            max_tokens=4096,
        )
    except Exception as e:
        logger.error(f"LLM ({provider.name}) ошибка: {e}")
        return f"Ошибка AI: {e}. Попробуйте ещё раз."
