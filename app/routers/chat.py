from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates
from app.models import Meeting, Folder, ChatMessage, ChatRole
from app.services.chat_engine import ask_about_meeting, ask_about_folder

router = APIRouter()


@router.get("/meetings/{meeting_id}/chat/history", response_class=HTMLResponse)
async def chat_history(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
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
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Встреча не найдена")

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
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Папка не найдена")

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
