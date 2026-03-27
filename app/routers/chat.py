import json
import logging

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates, get_current_user_optional, get_user_meeting, get_user_folder
from app.models import Meeting, Folder, ChatMessage, ChatRole
from app.services.chat_engine import (
    ask_about_meeting, ask_about_folder,
    _build_meeting_context, MEETING_SYSTEM, FOLDER_SYSTEM, MAX_HISTORY,
)
from app.services.providers.registry import get_provider_for_text

logger = logging.getLogger(__name__)

router = APIRouter()

# ---- Шаблоны AI-чата ----

CHAT_TEMPLATES = {
    "follow_up": {
        "name": "Follow Up",
        "icon": "✅",
        "description": "Задачи, ответственные и следующие шаги",
        "prompt": "Определи задачи, ответственных и следующие шаги по встрече.",
        "system": """Ты анализируешь транскрипт встречи. Извлеки ВСЕ задачи и определи ответственных.

Формат ответа:
## ✅ Задачи и ответственные

Для каждой задачи:
- [ ] **Задача** → Ответственный | Срок (если указан)

В конце:
## 📌 Следующие шаги
- Краткий список ближайших действий

КРИТИЧЕСКИ ВАЖНО: используй ТОЛЬКО информацию из транскрипта. Пиши на русском.""",
    },
    "summary": {
        "name": "Резюме встречи",
        "icon": "📋",
        "description": "Структурированное резюме: темы, решения, задачи",
        "prompt": "Дай структурированное резюме встречи: ключевые темы, решения, задачи.",
        "system": """Составь структурированное резюме встречи.

Формат ответа (используй эти секции):
## 📋 Резюме встречи
Дата, участники, длительность.

## 🧭 Темы обсуждения
- **Тема** — краткое описание

## 🧩 Принятые решения
- ✅ Решение (ключевые даты/термины **жирным**)

## ✅ Задачи
- [ ] Задача → Ответственный

## 📌 Ключевые акценты
- Самое важное, что нельзя упустить

Используй emoji-заголовки. Ключевую информацию выделяй **жирным**. Пиши на русском.""",
    },
    "protocol": {
        "name": "Протокол",
        "icon": "📄",
        "description": "Формальный протокол для шаринга",
        "prompt": "Составь формальный протокол встречи: повестка, участники, решения, задачи с дедлайнами.",
        "system": """Составь формальный протокол встречи в деловом стиле.

Формат:
## 📄 Протокол встречи

**Дата:** ...
**Участники:** ...
**Длительность:** ...

### Повестка
1. ...

### Обсуждение
По каждому пункту повестки — что обсуждалось, какие аргументы.

### Постановили
1. ...

### Сроки исполнения
| Задача | Ответственный | Срок |
|---|---|---|

Деловой стиль, русский язык. Только факты из транскрипта.""",
    },
    "tasks": {
        "name": "Трекер задач",
        "icon": "📊",
        "description": "Таблица задач с ответственными и дедлайнами",
        "prompt": "Извлеки все задачи, назначь ответственных и дедлайны.",
        "system": """Извлеки ВСЕ задачи из встречи и оформи в таблицу.

Формат:
## 📊 Трекер задач

| # | Задача | Ответственный | Дедлайн | Приоритет |
|---|---|---|---|---|
| 1 | ... | ... | ... | Высокий/Средний/Низкий |

Если дедлайн не указан явно — напиши «Не определён».
Если ответственный не назначен — напиши «Не назначен».

В конце:
## 📌 Итого
- Всего задач: N
- С дедлайнами: N
- Без ответственного: N

Русский язык. Только факты из транскрипта.""",
    },
    "daily": {
        "name": "Отчёт за день",
        "icon": "📊",
        "description": "Сводка по всем встречам за день",
        "prompt": "Составь сводку по всем встречам: итоги, задачи, ключевые решения.",
        "system": """Составь ежедневный отчёт по встречам.

Формат:
## 📊 Отчёт за день

### Общий итог
2-3 предложения: чем занимались, что важного.

### Встречи
По каждой встрече:
#### 📅 [Название]
- **Суть:** 1-2 предложения
- **Решения:** ключевые решения
- **Задачи:** задачи с ответственными

### ✅ Все задачи дня
Сводный чеклист всех задач из всех встреч.

### 📌 Приоритеты
Что нужно сделать в первую очередь.

Кратко, по делу, русский язык.""",
    },
}


@router.get("/meetings/{meeting_id}/chat/history", response_class=HTMLResponse)
async def chat_history(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Verify meeting ownership
    get_user_meeting(meeting_id, user, db)

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.meeting_id == meeting_id)
        .order_by(ChatMessage.created_at)
        .all()
    )
    return templates.TemplateResponse("partials/chat_message.html", {
        "request": request,
        "messages": messages,
    })


@router.delete("/meetings/{meeting_id}/chat", response_class=HTMLResponse)
async def clear_chat(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Verify meeting ownership
    get_user_meeting(meeting_id, user, db)

    db.query(ChatMessage).filter(ChatMessage.meeting_id == meeting_id).delete()
    db.commit()
    return HTMLResponse("")


@router.post("/meetings/{meeting_id}/chat", response_class=HTMLResponse)
async def chat_meeting(
    request: Request,
    meeting_id: int,
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)

    # Сохраняем вопрос пользователя
    user_msg = ChatMessage(
        meeting_id=meeting_id,
        role=ChatRole.user,
        content=message,
    )
    db.add(user_msg)
    db.commit()

    # Получаем историю чата
    history = (
        db.query(ChatMessage)
        .filter(ChatMessage.meeting_id == meeting_id)
        .order_by(ChatMessage.created_at)
        .all()
    )

    # Генерируем ответ
    answer = await ask_about_meeting(meeting, history)

    # Сохраняем ответ
    assistant_msg = ChatMessage(
        meeting_id=meeting_id,
        role=ChatRole.assistant,
        content=answer,
    )
    db.add(assistant_msg)
    db.commit()

    return templates.TemplateResponse("partials/chat_message.html", {
        "request": request,
        "messages": [user_msg, assistant_msg],
    })


@router.post("/folders/{folder_id}/chat", response_class=HTMLResponse)
async def chat_folder(
    request: Request,
    folder_id: int,
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    folder = get_user_folder(folder_id, user, db)

    user_msg = ChatMessage(
        folder_id=folder_id,
        role=ChatRole.user,
        content=message,
    )
    db.add(user_msg)
    db.commit()

    history = (
        db.query(ChatMessage)
        .filter(ChatMessage.folder_id == folder_id)
        .order_by(ChatMessage.created_at)
        .all()
    )

    answer = await ask_about_folder(folder, history)

    assistant_msg = ChatMessage(
        folder_id=folder_id,
        role=ChatRole.assistant,
        content=answer,
    )
    db.add(assistant_msg)
    db.commit()

    return templates.TemplateResponse("partials/chat_message.html", {
        "request": request,
        "messages": [user_msg, assistant_msg],
    })


# ---- Страница /chat ----

@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.name).all()
    meetings = (
        db.query(Meeting)
        .filter(Meeting.user_id == user.id)
        .order_by(Meeting.date.desc())
        .limit(50)
        .all()
    )

    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user": user,
        "folders": folders,
        "meetings": meetings,
        "chat_templates": CHAT_TEMPLATES,
    })


# ---- Streaming API ----

@router.post("/api/chat/stream")
async def chat_stream(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    message = body.get("message", "").strip()
    meeting_id = body.get("meeting_id")
    folder_id = body.get("folder_id")
    template_key = body.get("template")

    if not message:
        return StreamingResponse(
            iter([f"data: {json.dumps({'error': 'Пустое сообщение'})}\n\n"]),
            media_type="text/event-stream",
        )

    # System prompt: шаблон или дефолт
    if template_key and template_key in CHAT_TEMPLATES:
        system = CHAT_TEMPLATES[template_key]["system"]
    elif meeting_id:
        system = MEETING_SYSTEM
    elif folder_id:
        system = FOLDER_SYSTEM
    else:
        system = MEETING_SYSTEM

    # Контекст
    context = ""
    if meeting_id:
        meeting = db.query(Meeting).filter(
            Meeting.id == int(meeting_id), Meeting.user_id == user.id
        ).first()
        if meeting:
            context = _build_meeting_context(meeting)
    elif folder_id:
        folder = db.query(Folder).filter(
            Folder.id == int(folder_id), Folder.user_id == user.id
        ).first()
        if folder:
            parts = [f"**Проект:** {folder.name}\n**Встреч:** {len(folder.meetings)}"]
            for m in folder.meetings:
                info = f"\n---\n**{m.title}** ({m.date.strftime('%d.%m.%Y') if m.date else ''})"
                if m.summary:
                    info += f"\n{m.summary.tldr}"
                    if m.summary.tasks:
                        info += "\nЗадачи: " + "; ".join(t.get('task', '') for t in m.summary.tasks)
                elif m.transcript:
                    info += f"\n{m.transcript.full_text[:3000]}"
                parts.append(info)
            context = "\n".join(parts)[:100000]

    # LLM messages
    llm_messages = []
    if context:
        llm_messages.append({"role": "user", "content": f"Контекст:\n\n{context}"})
        llm_messages.append({"role": "assistant", "content": "Я изучил материалы. Задавайте вопросы."})

    # История
    if meeting_id:
        history = db.query(ChatMessage).filter(ChatMessage.meeting_id == int(meeting_id)).order_by(ChatMessage.created_at).all()
    elif folder_id:
        history = db.query(ChatMessage).filter(ChatMessage.folder_id == int(folder_id)).order_by(ChatMessage.created_at).all()
    else:
        history = []

    recent = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history
    for msg in recent:
        llm_messages.append({"role": msg.role.value, "content": msg.content})

    llm_messages.append({"role": "user", "content": message})

    # Сохраняем user message
    user_msg = ChatMessage(
        meeting_id=int(meeting_id) if meeting_id else None,
        folder_id=int(folder_id) if folder_id else None,
        role=ChatRole.user,
        content=message,
    )
    db.add(user_msg)
    db.commit()

    provider = get_provider_for_text(len(context))

    async def event_stream():
        full_response = ""
        try:
            async for chunk in provider.generate_stream(llm_messages, system=system, max_tokens=4096):
                full_response += chunk
                yield f"data: {json.dumps({'content': chunk})}\n\n"
        except Exception as e:
            logger.error(f"Stream error ({provider.name}): {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        # Сохраняем ответ
        try:
            assistant_msg = ChatMessage(
                meeting_id=int(meeting_id) if meeting_id else None,
                folder_id=int(folder_id) if folder_id else None,
                role=ChatRole.assistant,
                content=full_response or "Ошибка генерации",
            )
            db.add(assistant_msg)
            db.commit()
        except Exception as e:
            logger.error(f"Ошибка сохранения: {e}")

        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
