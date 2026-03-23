"""
JSON API для нативного macOS-клиента ZoomHub.
Все эндпоинты возвращают JSON, без HTML-шаблонов.
"""
import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import get_db
from app.models import Meeting, MeetingStatus, MeetingSource, Transcript, Summary, Folder, ChatMessage, ChatRole
from app.config import RECORDINGS_DIR, ALLOWED_EXTENSIONS, MAX_FILE_SIZE
from app.deps import get_current_user_optional
from app.services.pipeline import process_meeting
import app.config as config_module

router = APIRouter(prefix="/api")


# ──────────────── Сериализация ────────────────

def _meeting_dict(m: Meeting) -> dict:
    return {
        "id": m.id,
        "title": m.title,
        "date": m.date.isoformat() if m.date else str(m.created_at),
        "duration_seconds": m.duration_seconds,
        "source": m.source.value if m.source else "upload",
        "status": m.status.value,
        "audio_path": m.audio_path,
        "folder_id": m.folder_id,
        "folder_name": m.folder.name if m.folder else None,
        "created_at": str(m.created_at),
    }


def _transcript_dict(t: Transcript) -> dict | None:
    if not t:
        return None
    return {
        "id": t.id,
        "full_text": t.full_text,
        "segments": t.segments or [],
    }


def _summary_dict(s: Summary) -> dict | None:
    if not s:
        return None
    return {
        "id": s.id,
        "tldr": s.tldr,
        "tasks": s.tasks or [],
        "topics": s.topics or [],
        "insights": s.insights or [],
    }


def _folder_dict(f: Folder, count: int = 0) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "icon": f.icon,
        "keywords": f.keywords,
        "meeting_count": count,
    }


def _chat_dict(msg: ChatMessage) -> dict:
    return {
        "id": msg.id,
        "role": msg.role.value,
        "content": msg.content,
        "created_at": str(msg.created_at) if msg.created_at else None,
    }


# ──────────────── Meetings ────────────────

@router.get("/meetings")
async def list_meetings(
    q: str = Query("", description="Поиск"),
    status: str = Query("", description="Фильтр по статусу"),
    offset: int = Query(0, ge=0, description="Смещение"),
    limit: int = Query(50, ge=1, le=200, description="Количество"),
    db: Session = Depends(get_db),
):
    query = db.query(Meeting)

    if q and q.strip():
        pattern = f"%{q}%"
        query = (
            query.outerjoin(Transcript)
            .outerjoin(Summary)
            .filter(
                or_(
                    Meeting.title.ilike(pattern),
                    Transcript.full_text.ilike(pattern),
                    Summary.tldr.ilike(pattern),
                )
            )
        )

    if status and status != "all":
        query = query.filter(Meeting.status == status)

    meetings = query.order_by(Meeting.created_at.desc()).offset(offset).limit(limit).all()
    return [_meeting_dict(m) for m in meetings]


@router.get("/meetings/{meeting_id}/detail")
async def meeting_detail(meeting_id: int, db: Session = Depends(get_db)):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Встреча не найдена")

    return {
        "meeting": _meeting_dict(meeting),
        "transcript": _transcript_dict(meeting.transcript),
        "summary": _summary_dict(meeting.summary),
    }


@router.get("/meetings/{meeting_id}/progress")
async def meeting_progress(meeting_id: int, db: Session = Depends(get_db)):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        return {"status": "error", "progress": 0, "message": "Не найдена"}

    stages = {
        "downloading": {"progress": 10, "message": "Скачивание"},
        "transcribing": {"progress": 45, "message": "Транскрипция"},
        "summarizing": {"progress": 80, "message": "Генерация саммари"},
        "ready": {"progress": 100, "message": "Готово"},
        "error": {"progress": 0, "message": meeting.error_message or "Ошибка"},
    }
    info = stages.get(meeting.status.value, {"progress": 0, "message": ""})
    return {"status": meeting.status.value, **info}


@router.post("/meetings/upload")
async def upload_meeting(
    file: UploadFile = File(...),
    title: str = Form(""),
    folder_id: int = Form(None),
    db: Session = Depends(get_db),
):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=422, detail=f"Формат {ext} не поддерживается")

    if not title:
        title = Path(file.filename).stem

    meeting = Meeting(
        title=title,
        folder_id=folder_id if folder_id else None,
        source=MeetingSource.upload,
        status=MeetingStatus.transcribing,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    meeting_dir = RECORDINGS_DIR / str(meeting.id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    file_path = meeting_dir / f"original{ext}"

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    meeting.audio_path = str(file_path)
    db.commit()

    from app.services.pipeline import process_meeting
    asyncio.create_task(process_meeting(meeting.id))

    return _meeting_dict(meeting)


@router.patch("/meetings/{meeting_id}")
async def update_meeting(
    meeting_id: int,
    title: str = Form(None),
    folder_id: int = Form(None),
    db: Session = Depends(get_db),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    if title is not None:
        meeting.title = title
    if folder_id is not None:
        meeting.folder_id = folder_id
    db.commit()
    db.refresh(meeting)
    return _meeting_dict(meeting)


@router.delete("/meetings/{meeting_id}")
async def delete_meeting(meeting_id: int, db: Session = Depends(get_db)):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Встреча не найдена")

    if meeting.audio_path:
        meeting_dir = Path(meeting.audio_path).parent
        if meeting_dir.exists():
            shutil.rmtree(meeting_dir)

    db.delete(meeting)
    db.commit()
    return {"status": "ok"}


@router.post("/meetings/{meeting_id}/retry")
async def retry_meeting(meeting_id: int, db: Session = Depends(get_db)):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    if not meeting.audio_path:
        raise HTTPException(status_code=422, detail="Нет аудиофайла")

    meeting.status = MeetingStatus.transcribing
    meeting.error_message = None
    db.commit()

    from app.services.pipeline import process_meeting
    asyncio.create_task(process_meeting(meeting.id))

    return _meeting_dict(meeting)


@router.post("/meetings/{meeting_id}/resummarize")
async def resummarize_meeting(meeting_id: int, db: Session = Depends(get_db)):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    if not meeting.transcript:
        raise HTTPException(status_code=422, detail="Нет транскрипта")

    meeting.status = MeetingStatus.summarizing
    db.commit()

    from app.routers.meetings import _generate_summary_for_meeting
    asyncio.create_task(_generate_summary_for_meeting(meeting.id))

    return _meeting_dict(meeting)


# ──────────────── Chat ────────────────

@router.get("/meetings/{meeting_id}/chat/history")
async def chat_history(meeting_id: int, db: Session = Depends(get_db)):
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.meeting_id == meeting_id)
        .order_by(ChatMessage.created_at)
        .all()
    )
    return [_chat_dict(m) for m in messages]


@router.post("/meetings/{meeting_id}/chat")
async def chat_meeting(
    meeting_id: int,
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Встреча не найдена")

    user_msg = ChatMessage(
        meeting_id=meeting_id,
        role=ChatRole.user,
        content=message,
    )
    db.add(user_msg)
    db.commit()

    history = (
        db.query(ChatMessage)
        .filter(ChatMessage.meeting_id == meeting_id)
        .order_by(ChatMessage.created_at)
        .all()
    )

    from app.services.chat_engine import ask_about_meeting
    answer = await ask_about_meeting(meeting, history)

    assistant_msg = ChatMessage(
        meeting_id=meeting_id,
        role=ChatRole.assistant,
        content=answer,
    )
    db.add(assistant_msg)
    db.commit()

    return _chat_dict(assistant_msg)


@router.delete("/meetings/{meeting_id}/chat")
async def clear_chat(meeting_id: int, db: Session = Depends(get_db)):
    db.query(ChatMessage).filter(ChatMessage.meeting_id == meeting_id).delete()
    db.commit()
    return {"status": "ok"}


# ──────────────── Folders ────────────────

@router.get("/folders")
async def list_folders(db: Session = Depends(get_db)):
    folders = db.query(Folder).order_by(Folder.created_at.desc()).all()
    result = []
    for f in folders:
        count = db.query(Meeting).filter(Meeting.folder_id == f.id).count()
        result.append(_folder_dict(f, count))
    return result


@router.post("/folders")
async def create_folder(
    name: str = Form(...),
    icon: str = Form("📁"),
    keywords: str = Form(""),
    db: Session = Depends(get_db),
):
    folder = Folder(name=name, icon=icon, keywords=keywords)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return _folder_dict(folder)


@router.delete("/folders/{folder_id}")
async def delete_folder(folder_id: int, db: Session = Depends(get_db)):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    db.delete(folder)
    db.commit()
    return {"status": "ok"}


# ──────────────── Settings ────────────────

@router.post("/settings/llm-provider")
async def switch_llm(provider: str = Form(...)):
    if provider not in ("claude", "ollama", "auto"):
        raise HTTPException(status_code=400, detail="Неизвестный провайдер")
    config_module.LLM_PROVIDER = provider
    from app.services.providers.registry import reset_llm_provider
    reset_llm_provider()
    return {"status": "ok", "provider": provider}


@router.post("/settings/transcription-provider")
async def switch_transcription(provider: str = Form(...)):
    if provider not in ("bukvitsa", "whisper"):
        raise HTTPException(status_code=400, detail="Неизвестный провайдер")
    config_module.TRANSCRIPTION_PROVIDER = provider
    from app.services.providers.registry import reset_transcription_provider
    reset_transcription_provider()
    return {"status": "ok", "provider": provider}


@router.get("/settings/health/{provider_type}")
async def provider_health(provider_type: str):
    """Проверяет здоровье провайдера: ollama, claude, bukvitsa, whisper."""
    try:
        if provider_type == "ollama":
            from app.services.providers.ollama_provider import OllamaProvider
            p = OllamaProvider()
            ok = await p.health_check()
            return {"healthy": ok, "message": "Ollama доступна" if ok else "Ollama недоступна"}
        elif provider_type == "claude":
            from app.services.providers.claude_provider import ClaudeProvider
            p = ClaudeProvider()
            ok = await p.health_check()
            return {"healthy": ok, "message": "API ключ найден" if ok else "API ключ не задан"}
        elif provider_type == "bukvitsa":
            from app.services.providers.bukvitsa_provider import BukvitsaProvider
            p = BukvitsaProvider()
            ok = await p.health_check()
            return {"healthy": ok, "message": "Telegram сессия активна" if ok else "Сессия не найдена"}
        elif provider_type == "whisper":
            from app.services.providers.whisper_provider import WhisperProvider
            p = WhisperProvider()
            ok = await p.health_check()
            return {"healthy": ok, "message": "Whisper доступен" if ok else "Whisper не установлен"}
        else:
            raise HTTPException(status_code=400, detail="Неизвестный тип провайдера")
    except Exception as e:
        return {"healthy": False, "message": str(e)}


# ──────────────── Agent Upload ────────────────

@router.post("/agent/upload")
async def agent_upload(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    db: Session = Depends(get_db),
):
    """Upload recording from local agent. Uses JWT auth via Authorization header."""
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(401, "Invalid or missing API token")

    # Same logic as meetings upload but with source tracking
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    if not title:
        title = Path(file.filename).stem

    meeting = Meeting(
        user_id=user.id,
        title=title,
        source=MeetingSource.upload,
        status=MeetingStatus.transcribing,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    save_dir = RECORDINGS_DIR / str(meeting.id)
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f"original{ext}"

    with open(save_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1 MB chunks
            f.write(chunk)

    meeting.audio_path = str(save_path)
    db.commit()

    asyncio.create_task(process_meeting(meeting.id))

    return {"id": meeting.id, "title": meeting.title, "status": meeting.status.value}
