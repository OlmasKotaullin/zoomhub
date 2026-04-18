"""AI-чат по встречам через LLM-провайдер с fallback-цепочкой."""

import logging

from app.models import Meeting, Folder, ChatMessage
from app.services.providers.registry import get_provider_for_text, get_chat_provider_chain

logger = logging.getLogger(__name__)

MAX_HISTORY = 20  # последних сообщений в контексте

# Лимиты контекста в символах для каждого провайдера
_CONTEXT_LIMITS = {
    "gemini": 500000,
    "groq": 80000,
    "deepseek": 100000,
    "openrouter": 50000,
    "claude": 150000,
    "gigachat": 8000,
    "ollama": 12000,
}

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

TELEGRAM_MEETING_SYSTEM = """Ты — AI-ассистент для анализа рабочих встреч. Тебе предоставлен РЕАЛЬНЫЙ транскрипт и конспект встречи.

ПРАВИЛА:
- Отвечай ТОЛЬКО на основе контекста встречи
- НЕ выдумывай, чего нет в транскрипте
- Цитируй участников когда уместно (в кавычках)
- Называй реальные имена из транскрипта

ФОРМАТ (Telegram):
- Используй *жирный* для ключевых фраз (звёздочки ВСЕГДА парные)
- Списки через • или 1. 2. 3.
- Задачи: задача, ответственный
- Разделяй блоки пустой строкой
- НЕ используй заголовки (# ##) — Telegram их не рендерит
- НЕ используй чекбоксы (- [ ]) — Telegram их не рендерит
- ЗАПРЕЩЕНО: markdown-таблицы (символ |) — Telegram их не рендерит
- ЗАПРЕЩЕНО: _курсив_ (одиночные подчёркивания ломают парсинг)
- ЗАПРЕЩЕНО: `код` и ```блоки кода``` (обратные кавычки ломают парсинг)
- ЗАПРЕЩЕНО: [ссылки](url) — квадратные скобки ломают парсинг
- Пиши кратко: максимум 2000 символов если не просят больше
- Пиши на русском"""

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
        text = meeting.transcript.full_text
        # Safety limit — не передавать больше 500K символов
        if len(text) > 500000:
            text = text[:500000] + f"\n\n[...обрезано, показано 500K из {len(text)} символов]"
        parts.append(f"**Транскрипт:**\n{text}")

    return "\n\n".join(parts)


async def ask_about_meeting(meeting: Meeting, history: list[ChatMessage],
                            is_telegram: bool = False) -> str:
    """Отвечает на вопрос по конкретной встрече."""
    context = _build_meeting_context(meeting)
    system = TELEGRAM_MEETING_SYSTEM if is_telegram else MEETING_SYSTEM

    messages = [
        {"role": "user", "content": f"Контекст встречи:\n\n{context}"},
        {"role": "assistant", "content": "Я изучил материалы встречи. Задавайте вопросы."},
    ]

    recent = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history
    for msg in recent:
        messages.append({"role": msg.role.value, "content": msg.content})

    return await _call_llm(system, messages, context_length=len(context))


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
    """Вызывает LLM с fallback-цепочкой.

    Gemini (900K) → Groq (128K) → OpenRouter → DeepSeek → Claude → GigaChat
    Пропускает провайдеры, чей контекст слишком мал для данного транскрипта.
    """
    chain = get_chat_provider_chain()

    for provider in chain:
        max_chars = _CONTEXT_LIMITS.get(provider.name, 20000)

        # Пропустить провайдер если контекст слишком большой
        if context_length > max_chars * 1.5:
            logger.info(f"Chat: skip {provider.name} — context {context_length} > limit {max_chars}")
            continue

        try:
            result = await provider.generate(
                messages=messages,
                system=system,
                max_tokens=4096,
            )
            logger.info(f"Chat: {provider.name} OK, {len(result)} chars")
            return result
        except Exception as e:
            logger.warning(f"Chat: {provider.name} error: {e}, trying next...")

    return "AI-провайдеры временно недоступны. Попробуйте через минуту."
