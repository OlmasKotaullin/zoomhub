import json
import logging

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates, get_current_user_optional, get_user_meeting, get_user_folder
from app.models import Meeting, Folder, ChatMessage, ChatRole, ChatSession, FolderRole
from app.services.chat_engine import ask_about_meeting, ask_about_folder
from app.services.chat_context import build_chat_messages, get_system_prompt, DEFAULT_SYSTEM_PROMPT
from app.services.providers.registry import make_provider_by_name, get_available_providers

logger = logging.getLogger(__name__)

router = APIRouter()


# ---- Вспомогательные функции ----

def _resolve_provider(user, provider_name: str | None = None):
    """Определяет провайдер и API ключ для пользователя."""
    name = provider_name or user.user_llm_provider or "auto"
    if name == "auto":
        if user.user_groq_api_key:
            name = "groq"
        elif user.user_gemini_api_key:
            name = "gemini"
        elif user.user_gigachat_auth_key:
            name = "gigachat"
        elif user.user_anthropic_api_key:
            name = "claude"
        else:
            name = "groq"

    key_map = {
        "groq": user.user_groq_api_key,
        "gemini": user.user_gemini_api_key,
        "gigachat": user.user_gigachat_auth_key,
        "claude": user.user_anthropic_api_key,
    }
    api_key = key_map.get(name)
    return name, api_key


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
async def chat_page(
    request: Request,
    folder_id: int | None = None,
    meeting_id: int | None = None,
    session_id: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.name).all()
    providers = get_available_providers()

    current_session = None
    messages = []
    if session_id:
        current_session = db.query(ChatSession).filter(
            ChatSession.id == session_id, ChatSession.user_id == user.id
        ).first()
        if current_session:
            folder_id = current_session.folder_id
            meeting_id = current_session.meeting_id
            messages = db.query(ChatMessage).filter(
                ChatMessage.session_id == session_id
            ).order_by(ChatMessage.created_at).all()

    sessions_query = db.query(ChatSession).filter(ChatSession.user_id == user.id)
    if folder_id:
        sessions_query = sessions_query.filter(ChatSession.folder_id == folder_id)
    sessions = sessions_query.order_by(ChatSession.updated_at.desc()).limit(50).all()

    role_name = ""
    role_prompt = ""
    folder_name = ""
    if folder_id:
        role = db.query(FolderRole).filter(
            FolderRole.folder_id == folder_id, FolderRole.user_id == user.id
        ).first()
        if role:
            role_name = role.name
            role_prompt = role.system_prompt or ""
        folder_obj = db.query(Folder).filter(Folder.id == folder_id).first()
        if folder_obj:
            folder_name = f"{folder_obj.icon} {folder_obj.name}"

    # Meeting name for context
    meeting_name = ""
    if meeting_id:
        meeting_obj = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if meeting_obj:
            meeting_name = meeting_obj.title

    user_provider = user.user_llm_provider or "auto"

    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user": user,
        "folders": folders,
        "sessions": sessions,
        "current_session": current_session,
        "messages": messages,
        "folder_id": folder_id,
        "meeting_id": meeting_id,
        "providers": providers,
        "user_provider": user_provider,
        "role_name": role_name,
        "role_prompt": role_prompt,
        "folder_name": folder_name,
        "meeting_name": meeting_name,
    })


# ---- API: Сессии ----

@router.post("/api/chat/sessions")
async def create_session(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    folder_id = body.get("folder_id")
    meeting_id = body.get("meeting_id")
    provider = body.get("provider")

    session = ChatSession(
        user_id=user.id,
        folder_id=int(folder_id) if folder_id else None,
        meeting_id=int(meeting_id) if meeting_id else None,
        llm_provider=provider,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    return {"session_id": session.id, "title": session.title}


@router.get("/api/chat/sessions")
async def list_sessions(request: Request, folder_id: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401)

    query = db.query(ChatSession).filter(ChatSession.user_id == user.id)
    if folder_id:
        query = query.filter(ChatSession.folder_id == folder_id)

    sessions = query.order_by(ChatSession.updated_at.desc()).limit(50).all()
    return [
        {"id": s.id, "title": s.title, "folder_id": s.folder_id,
         "meeting_id": s.meeting_id, "provider": s.llm_provider,
         "created_at": s.created_at.isoformat() if s.created_at else None}
        for s in sessions
    ]


@router.delete("/api/chat/sessions/{session_id}")
async def delete_session(request: Request, session_id: int, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401)

    session = db.query(ChatSession).filter(
        ChatSession.id == session_id, ChatSession.user_id == user.id
    ).first()
    if not session:
        raise HTTPException(status_code=404)

    db.delete(session)
    db.commit()
    return {"ok": True}


# ---- API: Streaming сообщение ----

@router.post("/api/chat/message")
async def chat_message_stream(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    session_id = body.get("session_id")
    message = body.get("message", "").strip()
    provider_override = body.get("provider")

    if not session_id or not message:
        return JSONResponse({"error": "session_id и message обязательны"}, status_code=400)

    session = db.query(ChatSession).filter(
        ChatSession.id == session_id, ChatSession.user_id == user.id
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    # Сохраняем сообщение пользователя
    user_msg = ChatMessage(
        session_id=session.id,
        folder_id=session.folder_id,
        meeting_id=session.meeting_id,
        role=ChatRole.user,
        content=message,
    )
    db.add(user_msg)
    db.commit()

    # Автозаголовок из первого сообщения
    msg_count = db.query(ChatMessage).filter(ChatMessage.session_id == session.id).count()
    if msg_count <= 1 and session.title == "Новый чат":
        session.title = message[:80]
        db.commit()

    # Определяем провайдер
    provider_name, api_key = _resolve_provider(user, provider_override or session.llm_provider)
    if provider_override and provider_override != session.llm_provider:
        session.llm_provider = provider_override
        db.commit()

    # Собираем контекст
    system_prompt, llm_messages = build_chat_messages(session, db, provider_name)

    # Создаём провайдер
    provider = make_provider_by_name(provider_name, api_key)

    async def event_stream():
        full_response = ""
        try:
            async for chunk in provider.generate_stream(llm_messages, system=system_prompt, max_tokens=4096):
                full_response += chunk
                yield f"data: {json.dumps({'content': chunk})}\n\n"
        except Exception as e:
            logger.error(f"Stream error ({provider_name}): {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        # Сохраняем полный ответ
        from datetime import datetime, timezone
        try:
            assistant_msg = ChatMessage(
                session_id=session.id,
                folder_id=session.folder_id,
                meeting_id=session.meeting_id,
                role=ChatRole.assistant,
                content=full_response or "Ошибка генерации ответа",
            )
            db.add(assistant_msg)
            session.updated_at = datetime.now(timezone.utc)
            db.commit()
            yield f"data: {json.dumps({'done': True, 'message_id': assistant_msg.id})}\n\n"
        except Exception as e:
            logger.error(f"Ошибка сохранения ответа: {e}")
            yield f"data: {json.dumps({'done': True, 'error': 'save_failed'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- API: Роли ----

@router.get("/api/chat/role/{folder_id}")
async def get_role(request: Request, folder_id: int, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401)

    role = db.query(FolderRole).filter(
        FolderRole.folder_id == folder_id, FolderRole.user_id == user.id
    ).first()

    if role:
        return {"name": role.name, "system_prompt": role.system_prompt}
    return {"name": "", "system_prompt": ""}


@router.post("/api/chat/role")
async def save_role(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    folder_id = body.get("folder_id")
    name = body.get("name", "AI-ассистент")
    system_prompt = body.get("system_prompt", "")

    if not folder_id:
        return JSONResponse({"error": "folder_id обязателен"}, status_code=400)

    role = db.query(FolderRole).filter(
        FolderRole.folder_id == folder_id, FolderRole.user_id == user.id
    ).first()

    if role:
        role.name = name
        role.system_prompt = system_prompt
    else:
        role = FolderRole(
            folder_id=folder_id,
            user_id=user.id,
            name=name,
            system_prompt=system_prompt,
        )
        db.add(role)

    db.commit()
    return {"ok": True}


@router.post("/api/chat/role/generate")
async def generate_role(request: Request, db: Session = Depends(get_db)):
    """AI генерирует system prompt из краткого описания бизнеса."""
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401)

    body = await request.json()
    description = body.get("description", "").strip()

    if not description:
        return JSONResponse({"error": "Опишите ваш бизнес"}, status_code=400)

    provider_name, api_key = _resolve_provider(user)
    provider = make_provider_by_name(provider_name, api_key)

    gen_system = """Ты — эксперт по созданию system prompt для AI-ассистентов.
На основе описания бизнеса создай подробный system prompt на русском языке.
Включи: роль ассистента, информацию о компании, направления/услуги, правила общения, ограничения.
Формат: структурированный markdown с заголовками ## и списками.
Ответ — только текст промта, без обёрток и комментариев."""

    gen_messages = [{"role": "user", "content": f"Описание бизнеса:\n{description}"}]

    async def event_stream():
        try:
            async for chunk in provider.generate_stream(gen_messages, system=gen_system, max_tokens=2048):
                yield f"data: {json.dumps({'content': chunk})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
