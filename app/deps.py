"""Common dependencies: templates, auth, DB helpers."""

from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import decode_token
from app.database import get_db

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_current_user_optional(request: Request, db: Session = Depends(get_db)):
    """Return User or None — for routes that work both ways."""
    from app.models import User

    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        return None

    user_id = decode_token(token)
    if not user_id:
        return None

    return db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712


def get_current_user(request: Request, db: Session = Depends(get_db)):
    """Return User or redirect to /login (HTML) / 401 (API)."""
    user = get_current_user_optional(request, db)
    if user:
        return user

    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=401, detail="Не авторизован")

    return RedirectResponse("/login", status_code=302)


def get_user_meeting(meeting_id: int, user, db: Session):
    """Get meeting owned by user or raise 404."""
    from app.models import Meeting

    meeting = db.query(Meeting).filter(
        Meeting.id == meeting_id, Meeting.user_id == user.id
    ).first()
    if not meeting:
        raise HTTPException(404, "Встреча не найдена")
    return meeting


def get_user_folder(folder_id: int, user, db: Session):
    """Get folder owned by user or raise 404."""
    from app.models import Folder

    folder = db.query(Folder).filter(
        Folder.id == folder_id, Folder.user_id == user.id
    ).first()
    if not folder:
        raise HTTPException(404, "Папка не найдена")
    return folder
