"""Zoom роутер — статус polling и ручной триггер проверки."""

import asyncio

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates
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
