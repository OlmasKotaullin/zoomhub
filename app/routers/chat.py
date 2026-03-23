from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates, get_current_user_optional, get_user_meeting, get_user_folder
from app.models import Meeting, Folder, ChatMessage, ChatRole
from app.services.chat_engine import ask_about_meeting, ask_about_folder

router = APIRouter()


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
