import json
import logging

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates, get_current_user, get_current_user_optional, get_user_meeting, get_user_folder
from app.models import Meeting, MeetingStatus, Folder, ChatMessage, ChatRole, User
from app.services.chat_engine import (
    ask_about_meeting, ask_about_folder,
    _build_meeting_context, MEETING_SYSTEM, FOLDER_SYSTEM, MAX_HISTORY,
)
from app.services.providers.registry import get_provider_for_text

logger = logging.getLogger(__name__)

router = APIRouter()

# ---- Скиллы пользователя ----

SKILL_PROMPTS = {
    "marketing_strategist": """## СКИЛЛ: Маркетолог-стратег
Анализируй встречи как опытный маркетолог. Используй:
- Фреймворк «Знаю → Хочу → Верю → Плачу» для оценки воронки
- 4-блочный аудит: продажи, юнит-экономика, воронки, трафик
- Дорожная карта: аудитория/продукт → упаковка/реклама → инфраструктура
- Квалификация лидов A/B/C/D
- Конкретные рекомендации с цифрами и ЦКП (ценный конечный продукт)
- Диагностика: CPL, CAC, LTV, конверсии, средний чек""",

    "sales_analyst": """## СКИЛЛ: Аналитик продаж
Анализируй встречи с фокусом на продажи:
- KPI отдела продаж: конверсия, средний чек, LTV, цикл сделки
- Качество лидов и квалификация (A/B/C/D)
- Скрипты продаж — что работает, что нет
- CRM: как используется, что упускается
- Воронка продаж: где теряются клиенты
- Конкретные рекомендации по улучшению конверсии""",

    "hr_manager": """## СКИЛЛ: HR-менеджер
Анализируй встречи с фокусом на людей и команду:
- Командная динамика: кто лидирует, кто блокирует
- Распределение ролей и ответственности
- Мотивация и вовлечённость участников
- Конфликты и их решение
- Рекомендации по найму, обучению, структуре
- Оценка эффективности 1-on-1 и планёрок""",

    "project_manager": """## СКИЛЛ: Проджект-менеджер
Анализируй встречи с фокусом на управление проектами:
- Извлекай ВСЕ задачи с дедлайнами и ответственными
- Оценивай риски и блокеры
- Отслеживай статусы: что сделано, что в работе, что просрочено
- Приоритизация: что критично, что можно отложить
- Формируй action items в формате: задача | ответственный | дедлайн | приоритет
- Рекомендации по процессам (Agile, Kanban, спринты)""",

    "content_creator": """## СКИЛЛ: Контент-мейкер
Извлекай из встреч материал для контента:
- Идеи для постов (Telegram, блог, соцсети)
- Кейсы для портфолио (было → стало → результат)
- Экспертные темы для статей
- Цитаты и инсайты участников
- Для каждой идеи: тема, формат, ключевая мысль, тезисы
- Контент должен быть полезным и конкретным""",
}


def _build_skills_prompt(active_skills: list) -> str:
    """Собрать промпт из активных скиллов."""
    parts = []
    for skill_id in active_skills:
        if skill_id in SKILL_PROMPTS:
            parts.append(SKILL_PROMPTS[skill_id])
    return "\n\n".join(parts)


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
        user_id=user.id,
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
        user_id=user.id,
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

    response = templates.TemplateResponse("chat.html", {
        "request": request,
        "user": user,
        "folders": folders,
        "meetings": meetings,
        "chat_templates": CHAT_TEMPLATES,
    })
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


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
        async def empty_error():
            yield f"data: {json.dumps({'error': 'Пустое сообщение'})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        return StreamingResponse(empty_error(), media_type="text/event-stream")

    # Validate IDs
    try:
        mid = int(meeting_id) if meeting_id else None
        fid = int(folder_id) if folder_id else None
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Неверный meeting_id или folder_id")

    # System prompt: шаблон или дефолт
    if template_key and template_key in CHAT_TEMPLATES:
        system = CHAT_TEMPLATES[template_key]["system"]
    elif mid:
        system = MEETING_SYSTEM
    elif fid:
        system = FOLDER_SYSTEM
    else:
        system = MEETING_SYSTEM

    # Пользовательская прокачка: скиллы + промпт + память + знания
    try:
        active_skills = getattr(user, "claude_active_skills", None) or []
        if active_skills:
            system += "\n\n" + _build_skills_prompt(active_skills)

        user_prompt = getattr(user, "claude_system_prompt", None)
        if user_prompt:
            system += f"\n\n## ИНСТРУКЦИИ ПОЛЬЗОВАТЕЛЯ\n{user_prompt}"

        user_knowledge = getattr(user, "claude_knowledge_text", None)
        if user_knowledge:
            system += f"\n\n## БАЗА ЗНАНИЙ ПОЛЬЗОВАТЕЛЯ\n{user_knowledge[:5000]}"

        user_memories = getattr(user, "claude_memories", None)
        if user_memories:
            system += "\n\n## ПАМЯТЬ\nЗапомненные факты:\n" + "\n".join(f"- {m}" for m in user_memories)
    except Exception as e:
        logger.warning(f"Ошибка прокачки: {e}")

    # Контекст (с проверкой владельца — обнуляем ID если не найден)
    context = ""
    if mid:
        meeting = db.query(Meeting).filter(Meeting.id == mid, Meeting.user_id == user.id).first()
        if meeting:
            context = _build_meeting_context(meeting)
        else:
            mid = None  # встреча не найдена или не принадлежит пользователю
    elif fid:
        folder = db.query(Folder).filter(Folder.id == fid, Folder.user_id == user.id).first()
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
                if len("\n".join(parts)) > 100000:
                    break
            context = "\n".join(parts)[:100000]
        else:
            fid = None  # папка не найдена или не принадлежит пользователю
    else:
        # Автоконтекст: обзор всех встреч с резюме и задачами
        all_meetings = db.query(Meeting).filter(
            Meeting.user_id == user.id,
            Meeting.status == MeetingStatus.ready,
        ).order_by(Meeting.created_at.desc()).limit(20).all()
        if all_meetings:
            parts = [f"Все встречи пользователя ({len(all_meetings)}):"]
            for m in all_meetings:
                entry = f"\n---\n**{m.title}** | {m.date.strftime('%d.%m.%Y %H:%M') if m.date else '?'}"
                if m.folder:
                    entry += f" | Папка: {m.folder.name}"
                if m.summary:
                    if m.summary.tldr:
                        entry += f"\nРезюме: {m.summary.tldr}"
                    if m.summary.tasks:
                        tasks_str = "\n".join(f"  - {t.get('task', '')}" for t in m.summary.tasks[:10])
                        entry += f"\nЗадачи ({len(m.summary.tasks)}):\n{tasks_str}"
                    if m.summary.topics:
                        topic_names = [t if isinstance(t, str) else t.get('topic', t.get('name', str(t))) for t in m.summary.topics[:5]]
                        entry += f"\nТемы: {', '.join(topic_names)}"
                parts.append(entry)
                if len("\n".join(parts)) > 60000:
                    break
            context = "\n".join(parts)
            system = """Ты — AI Companion ZoomHub, умный ассистент по встречам.

У тебя есть полный доступ ко всем встречам пользователя: резюме, задачи, темы, участники.

Правила:
- Отвечай на русском, структурированно, с markdown
- Если спрашивают про дату — найди встречи за эту дату и дай детальный ответ
- Если просят резюме за период — объедини данные из всех встреч за период
- Если просят задачи — собери задачи из всех встреч, сгруппируй по ответственным
- Если просят найти тему — ищи по резюме и темам встреч
- Используй таблицы markdown для структурированных данных
- Не придумывай информацию — используй только данные из контекста
- Если информации нет — так и скажи"""

    # LLM messages
    llm_messages = []
    if context:
        llm_messages.append({"role": "user", "content": f"Контекст:\n\n{context}"})
        llm_messages.append({"role": "assistant", "content": "Я изучил материалы. Задавайте вопросы."})

    # История
    if mid:
        history = db.query(ChatMessage).filter(ChatMessage.meeting_id == mid).order_by(ChatMessage.created_at).all()
    elif fid:
        history = db.query(ChatMessage).filter(ChatMessage.folder_id == fid).order_by(ChatMessage.created_at).all()
    else:
        history = []

    recent = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history
    for msg in recent:
        llm_messages.append({"role": msg.role.value, "content": msg.content})

    llm_messages.append({"role": "user", "content": message})

    # Сохраняем user message
    user_msg = ChatMessage(user_id=user.id, meeting_id=mid, folder_id=fid, role=ChatRole.user, content=message)
    db.add(user_msg)
    db.commit()

    from app.services.providers.registry import make_provider_by_name, get_user_keys
    user_keys = get_user_keys(user)

    # Для не-admin: использовать только пользовательские ключи (серверные — только для admin)
    is_admin = getattr(user, "is_admin", False)
    if is_admin:
        provider = get_provider_for_text(len(context))
    elif user_keys:
        # Пользователь имеет свои ключи — выбрать первый доступный
        key_to_provider = {"gigachat": "gigachat", "groq": "groq", "gemini": "gemini", "claude": "claude", "anthropic": "claude"}
        provider = None
        for key_name, prov_name in key_to_provider.items():
            if user_keys.get(key_name):
                provider = make_provider_by_name(prov_name, user_keys=user_keys)
                break
        if not provider:
            provider = get_provider_for_text(len(context))
    else:
        # Нет ключей — используем серверный провайдер (для первых пользователей / демо)
        provider = get_provider_for_text(len(context))

    # Если у пользователя есть свой ключ для выбранного провайдера — пересоздаём с ним
    if user_keys.get(provider.name):
        provider = make_provider_by_name(provider.name, user_keys=user_keys)
    logger.info(f"Chat stream: user={user.id}, meeting={mid}, folder={fid}, provider={provider.name}, template={template_key}")

    async def event_stream():
        full_response = ""
        # Цепочка провайдеров: основной → Gemini → Claude
        from app.config import GOOGLE_AI_API_KEY, ANTHROPIC_API_KEY
        providers_chain = [provider]
        has_gemini = user_keys.get("gemini") or GOOGLE_AI_API_KEY
        has_claude = user_keys.get("claude") or ANTHROPIC_API_KEY
        if provider.name != "gemini" and has_gemini:
            providers_chain.append(make_provider_by_name("gemini", user_keys=user_keys))
        if provider.name != "claude" and has_claude:
            providers_chain.append(make_provider_by_name("claude", user_keys=user_keys))

        last_error = None
        for p in providers_chain:
            try:
                logger.info(f"Trying provider: {p.name}")
                async for chunk in p.generate_stream(llm_messages, system=system, max_tokens=8192):
                    full_response += chunk
                    yield f"data: {json.dumps({'content': chunk})}\n\n"
                last_error = None
                break  # успех — выходим из цикла
            except Exception as e:
                logger.warning(f"Provider {p.name} failed: {e}")
                last_error = e
                continue  # пробуем следующий

        if last_error:
            yield f"data: {json.dumps({'error': 'Все провайдеры недоступны. Попробуйте позже.'})}\n\n"

        # Сохраняем ответ в отдельной сессии БД (stream может пережить основную)
        from app.database import SessionLocal
        save_db = SessionLocal()
        try:
            assistant_msg = ChatMessage(
                user_id=user.id, meeting_id=mid, folder_id=fid,
                role=ChatRole.assistant,
                content=full_response or "Ошибка генерации",
            )
            save_db.add(assistant_msg)
            save_db.commit()
            saved_msg_id = assistant_msg.id
        except Exception as e:
            logger.error(f"Ошибка сохранения ответа: {e}")
            save_db.rollback()
            saved_msg_id = None
        finally:
            save_db.close()

        yield f"data: {json.dumps({'done': True, 'message_id': saved_msg_id})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- Редактирование AI-сообщений ----

@router.put("/api/chat/messages/{message_id}")
async def update_chat_message(
    message_id: int,
    request: Request,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    body = await request.json()
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "Текст не может быть пустым")
    if len(content) > 50000:
        raise HTTPException(400, "Текст слишком длинный")

    msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")

    if msg.role != ChatRole.assistant:
        raise HTTPException(403, "Можно редактировать только AI-ответы")

    # Проверяем ownership: напрямую через user_id или через meeting/folder
    owned = False
    if msg.user_id and msg.user_id == user.id:
        owned = True
    elif msg.meeting_id:
        meeting = db.query(Meeting).filter(Meeting.id == msg.meeting_id).first()
        if meeting and meeting.user_id == user.id:
            owned = True
    elif msg.folder_id:
        folder = db.query(Folder).filter(Folder.id == msg.folder_id).first()
        if folder and folder.user_id == user.id:
            owned = True
    if not owned:
        raise HTTPException(403, "Нет доступа")

    from datetime import datetime, timezone
    msg.content = content
    msg.edited_at = datetime.now(timezone.utc)
    db.commit()

    return {"ok": True, "edited_at": msg.edited_at.isoformat()}


# ---- Удаление AI-сообщений ----

@router.delete("/api/chat/messages/{message_id}")
async def delete_chat_message(
    message_id: int,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")

    if msg.role != ChatRole.assistant:
        raise HTTPException(403, "Можно удалить только AI-ответы")

    # Проверяем ownership
    owned = False
    if msg.user_id and msg.user_id == user.id:
        owned = True
    elif msg.meeting_id:
        meeting = db.query(Meeting).filter(Meeting.id == msg.meeting_id).first()
        if meeting and meeting.user_id == user.id:
            owned = True
    elif msg.folder_id:
        folder = db.query(Folder).filter(Folder.id == msg.folder_id).first()
        if folder and folder.user_id == user.id:
            owned = True
    if not owned:
        raise HTTPException(403, "Нет доступа")

    # Удаляем также предшествующее user-сообщение (пару вопрос-ответ)
    prev_user_msg = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.id < msg.id,
            ChatMessage.role == ChatRole.user,
            ChatMessage.meeting_id == msg.meeting_id,
            ChatMessage.folder_id == msg.folder_id,
        )
        .order_by(ChatMessage.id.desc())
        .first()
    )

    db.delete(msg)
    if prev_user_msg:
        db.delete(prev_user_msg)
    db.commit()

    return {"ok": True}
