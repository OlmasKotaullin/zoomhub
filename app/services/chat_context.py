"""Сборка контекста для AI-чата с настраиваемыми ролями."""

import logging

from sqlalchemy.orm import Session

from app.models import ChatMessage, ChatSession, Folder, FolderRole, Meeting

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "Ты — полезный AI-ассистент. Отвечай на русском языке. Используй markdown для структуры ответов."

CONTEXT_LIMITS = {
    "gemini": 900000,
    "groq": 100000,
    "gigachat": 25000,
    "claude": 100000,
    "ollama": 12000,
    "auto": 100000,
}

MAX_HISTORY = 30


def get_system_prompt(folder_id: int | None, user_id: int, db: Session) -> str:
    """Получает system prompt из FolderRole или возвращает дефолт."""
    if not folder_id:
        return DEFAULT_SYSTEM_PROMPT

    role = db.query(FolderRole).filter(
        FolderRole.folder_id == folder_id,
        FolderRole.user_id == user_id,
    ).first()

    if role and role.system_prompt and role.system_prompt.strip():
        return role.system_prompt

    return DEFAULT_SYSTEM_PROMPT


def build_meeting_context(meeting: Meeting, max_chars: int = 80000) -> str:
    """Собирает контекст одной встречи (саммари + транскрипт)."""
    parts = [f"**Встреча:** {meeting.title}"]

    if meeting.date:
        parts.append(f"**Дата:** {meeting.date.strftime('%d.%m.%Y %H:%M')}")

    if meeting.summary:
        s = meeting.summary
        parts.append(f"**Резюме:** {s.tldr}")

        if s.tasks:
            tasks_str = "\n".join(
                f"- {t.get('task', '')}" + (f" -> {t['assignee']}" if t.get('assignee') else "")
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
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[...обрезано, показано {max_chars} из {len(meeting.transcript.full_text)} символов]"
        parts.append(f"**Транскрипт:**\n{text}")

    return "\n\n".join(parts)


def build_folder_context(folder: Folder, meeting_ids: list[int] | None = None) -> str:
    """Собирает контекст всех (или выбранных) встреч папки — только саммари."""
    parts = [f"**Проект:** {folder.name}\n**Всего встреч:** {len(folder.meetings)}"]

    meetings = folder.meetings
    if meeting_ids is not None:
        meetings = [m for m in meetings if m.id in meeting_ids]

    for meeting in meetings:
        info = f"\n---\n**{meeting.title}** ({meeting.date.strftime('%d.%m.%Y') if meeting.date else ''})"
        if meeting.summary:
            s = meeting.summary
            info += f"\n{s.tldr}"
            if s.tasks:
                info += "\nЗадачи: " + "; ".join(t.get('task', '') for t in s.tasks)
            if s.topics:
                info += "\nТемы: " + ", ".join(t.get('topic', '') for t in s.topics)
        elif meeting.transcript:
            info += f"\n{meeting.transcript.full_text[:3000]}"
        parts.append(info)

    return "\n".join(parts)


def build_chat_messages(
    session: ChatSession,
    db: Session,
    provider_name: str = "auto",
    user_question: str | None = None,
) -> tuple[str, list[dict]]:
    """Собирает (system_prompt, messages) для отправки в LLM.

    Returns:
        (system_prompt, messages_list)
    """
    limit = CONTEXT_LIMITS.get(provider_name, 100000)

    # 1. System prompt
    system = get_system_prompt(session.folder_id, session.user_id, db)

    # 2. Контекст встречи или папки
    context_parts = []

    if session.meeting_id:
        meeting = db.query(Meeting).filter(Meeting.id == session.meeting_id).first()
        if meeting:
            transcript_max = min(limit // 2, 200000)
            context_parts.append(build_meeting_context(meeting, max_chars=transcript_max))
    elif session.folder_id:
        folder = db.query(Folder).filter(Folder.id == session.folder_id).first()
        if folder:
            context_parts.append(build_folder_context(folder))

    # 3. Собираем messages
    messages = []

    if context_parts:
        context_text = "\n\n".join(context_parts)
        if len(context_text) > limit:
            context_text = context_text[:limit] + "\n\n[...контекст обрезан]"
        messages.append({"role": "user", "content": f"Контекст:\n\n{context_text}"})
        messages.append({"role": "assistant", "content": "Я изучил материалы. Задавайте вопросы."})

    # 4. История сообщений сессии
    history = db.query(ChatMessage).filter(
        ChatMessage.session_id == session.id
    ).order_by(ChatMessage.created_at.asc()).all()

    recent = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history

    for msg in recent:
        messages.append({"role": msg.role.value, "content": msg.content})

    # 5. Новый вопрос (если передан отдельно)
    if user_question:
        messages.append({"role": "user", "content": user_question})

    return system, messages
