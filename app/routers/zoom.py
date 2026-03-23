"""Zoom роутер — статус polling, ручной триггер проверки, per-user OAuth."""

import asyncio

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates, get_current_user_optional
from app.services.zoom_client import is_configured

router = APIRouter(prefix="/zoom")

# Без prefix — доступен как /api/bukvitsa-usage
from fastapi import APIRouter as _AR
_api = _AR()


@_api.get("/api/bukvitsa-usage")
async def bukvitsa_usage(db: Session = Depends(get_db)):
    """Статистика использования Буквицы за текущий месяц."""
    from datetime import datetime, timezone
    from app.models import Meeting, MeetingStatus, Transcript

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Считаем длительность по количеству слов в транскриптах (150 слов/мин)
    meetings = (
        db.query(Meeting)
        .join(Transcript)
        .filter(Meeting.created_at >= month_start)
        .filter(Meeting.status == MeetingStatus.ready)
        .all()
    )

    total_minutes = 0
    for m in meetings:
        if m.transcript and m.transcript.full_text:
            words = len(m.transcript.full_text.split())
            total_minutes += words / 150  # ~150 слов/мин речи

    limit_hours = 30
    used_hours = total_minutes / 60
    left_hours = max(0, limit_hours - used_hours)
    percent = min(100, int(used_hours / limit_hours * 100))

    return {
        "used_hours": round(used_hours, 1),
        "limit_hours": limit_hours,
        "left_hours": round(left_hours, 1),
        "percent": percent,
        "meetings_count": len(meetings),
    }


@router.get("/status")
async def zoom_status():
    """Проверяет статус подключения Zoom."""
    return {
        "configured": is_configured(),
        "polling": is_configured(),
    }


@router.post("/check-now")
async def check_now():
    """Ручной триггер проверки новых записей (не дожидаясь polling)."""
    if not is_configured():
        raise HTTPException(status_code=400, detail="Zoom не настроен")

    from app.services.zoom_poller import _check_new_recordings
    await _check_new_recordings()

    return {"status": "ok", "message": "Проверка завершена"}


# ──────────────── Per-user Zoom OAuth ────────────────

@router.get("/auth/zoom")
async def zoom_oauth_login(request: Request, db: Session = Depends(get_db)):
    """Redirect to Zoom OAuth authorize URL."""
    from app.services.zoom_oauth import get_authorize_url
    redirect_uri = str(request.url_for("zoom_oauth_callback"))
    # Behind reverse proxy, force HTTPS
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    url = get_authorize_url(redirect_uri)
    return RedirectResponse(url)


@router.get("/auth/zoom/callback")
async def zoom_oauth_callback(request: Request, db: Session = Depends(get_db)):
    """Exchange Zoom OAuth code for tokens, save in user record."""
    from app.services.zoom_oauth import exchange_code, get_zoom_user_info

    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    code = request.query_params.get("code")
    if not code:
        return RedirectResponse("/", status_code=302)

    redirect_uri = str(request.url_for("zoom_oauth_callback"))
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)

    try:
        tokens = await exchange_code(code, redirect_uri)
        user.zoom_access_token = tokens["access_token"]
        user.zoom_refresh_token = tokens["refresh_token"]
        user.zoom_token_expires_at = tokens["expires_at"]

        # Get Zoom user email
        info = await get_zoom_user_info(tokens["access_token"])
        if info:
            user.zoom_user_email = info.get("email")

        db.commit()
    except Exception:
        pass

    return RedirectResponse("/", status_code=302)


@router.post("/api/zoom/disconnect")
async def zoom_disconnect(request: Request, db: Session = Depends(get_db)):
    """Clear user's Zoom tokens."""
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")

    user.zoom_access_token = None
    user.zoom_refresh_token = None
    user.zoom_token_expires_at = None
    user.zoom_user_email = None
    db.commit()

    return {"status": "ok"}


@router.get("/api/zoom/status")
async def zoom_user_status(request: Request, db: Session = Depends(get_db)):
    """Return whether user has Zoom connected."""
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")

    return {
        "connected": user.zoom_access_token is not None,
        "email": user.zoom_user_email,
        "capture_source": user.capture_source,
    }


# ──────────────── Debug ────────────────

@router.get("/debug/telegram")
async def debug_telegram():
    """Debug: показывает последние сообщения из чата с Буквицей."""
    from app.services.transcriber import _get_client
    from app.config import BUKVITSA_BOT_USERNAME

    try:
        client = await _get_client()
        bot = await client.get_entity(BUKVITSA_BOT_USERNAME)
        msgs = await client.get_messages(bot, limit=10)

        result = []
        for msg in msgs:
            doc_info = None
            if msg.document:
                doc_name = ""
                for attr in msg.document.attributes:
                    if hasattr(attr, 'file_name'):
                        doc_name = attr.file_name
                        break
                doc_info = {
                    "name": doc_name,
                    "mime": getattr(msg.document, 'mime_type', ''),
                    "size": getattr(msg.document, 'size', 0),
                }
            result.append({
                "id": msg.id,
                "date": str(msg.date),
                "out": msg.out,
                "text": (msg.text or "")[:300],
                "document": doc_info,
            })
        return {"messages": result}
    except Exception as e:
        return {"error": str(e)}


@router.get("/debug/download/{msg_id}")
async def debug_download(msg_id: int):
    """Debug: скачивает файл из сообщения Буквицы."""
    from app.services.transcriber import _get_client
    from app.config import BUKVITSA_BOT_USERNAME

    try:
        client = await _get_client()
        bot = await client.get_entity(BUKVITSA_BOT_USERNAME)
        msgs = await client.get_messages(bot, ids=[msg_id])
        if not msgs or not msgs[0]:
            return {"error": "Message not found"}
        msg = msgs[0]
        if not msg.document:
            return {"error": "No document in message"}

        data = await client.download_media(msg, bytes)
        if not data:
            return {"error": "download_media returned None"}

        try:
            text = data.decode('utf-8', errors='replace')
            return {"size": len(data), "text_len": len(text), "preview": text[:500]}
        except Exception:
            return {"size": len(data), "binary": True}
    except Exception as e:
        return {"error": str(e)}
