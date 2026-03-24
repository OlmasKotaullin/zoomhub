import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import get_db
from app.deps import templates, get_current_user_optional, get_user_meeting, get_user_folder
from app.models import Meeting, MeetingStatus, MeetingSource, Transcript, Summary, Folder
from app.config import RECORDINGS_DIR, ALLOWED_EXTENSIONS, MAX_FILE_SIZE

router = APIRouter(prefix="/meetings")


@router.get("/search", response_class=HTMLResponse)
async def search_meetings(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not q or not q.strip():
        return templates.TemplateResponse("partials/meeting_list.html", {
            "request": request,
            "meetings": [],
            "search_query": "",
        })

    pattern = f"%{q}%"
    meetings = (
        db.query(Meeting)
        .outerjoin(Transcript)
        .outerjoin(Summary)
        .filter(
            Meeting.user_id == user.id,
            or_(
                Meeting.title.ilike(pattern),
                Transcript.full_text.ilike(pattern),
                Summary.tldr.ilike(pattern),
            ),
        )
        .order_by(Meeting.created_at.desc())
        .all()
    )

    return templates.TemplateResponse("partials/meeting_list.html", {
        "request": request,
        "meetings": meetings,
        "search_query": q.strip(),
    })


@router.get("/filter", response_class=HTMLResponse)
async def filter_meetings(
    request: Request,
    status: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    query = db.query(Meeting).filter(Meeting.user_id == user.id)
    if status and status != "all":
        query = query.filter(Meeting.status == status)
    meetings = query.order_by(Meeting.created_at.desc()).all()

    return templates.TemplateResponse("partials/meeting_list.html", {
        "request": request,
        "meetings": meetings,
        "search_query": "",
    })


@router.post("/upload", response_class=HTMLResponse)
async def upload_meeting(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    folder_id: int = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=422, detail=f"Формат {ext} не поддерживается. Допустимые: {', '.join(ALLOWED_EXTENSIONS)}")

    if not title:
        title = Path(file.filename).stem

    # Validate folder ownership if folder_id provided
    if folder_id:
        get_user_folder(folder_id, user, db)

    meeting = Meeting(
        title=title,
        folder_id=folder_id if folder_id else None,
        source=MeetingSource.upload,
        status=MeetingStatus.transcribing,
        user_id=user.id,
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

    # Запуск pipeline в фоне
    from app.services.pipeline import process_meeting
    asyncio.create_task(process_meeting(meeting.id))

    return templates.TemplateResponse("partials/meeting_card.html", {
        "request": request,
        "meeting": meeting,
    })


@router.post("/add-text", response_class=HTMLResponse)
async def add_text_meeting(
    request: Request,
    title: str = Form(...),
    transcript_text: str = Form(...),
    folder_id: int = Form(None),
    db: Session = Depends(get_db),
):
    """Добавить встречу из текста (вставить транскрипт вручную)."""
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Validate folder ownership if folder_id provided
    if folder_id:
        get_user_folder(folder_id, user, db)

    meeting = Meeting(
        title=title,
        folder_id=folder_id if folder_id else None,
        source=MeetingSource.upload,
        status=MeetingStatus.summarizing,
        user_id=user.id,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    # Сохраняем транскрипт
    from app.services.transcriber import parse_response
    parsed = parse_response(transcript_text)

    transcript = Transcript(
        meeting_id=meeting.id,
        full_text=parsed["full_text"],
        segments=parsed["segments"],
    )
    db.add(transcript)
    db.commit()

    # Генерируем саммари в фоне
    asyncio.create_task(_generate_summary_for_meeting(meeting.id))

    return templates.TemplateResponse("partials/meeting_card.html", {
        "request": request,
        "meeting": meeting,
    })


async def _generate_summary_for_meeting(meeting_id: int):
    """Фоновая генерация саммари для встречи."""
    from app.database import SessionLocal
    from app.services.summarizer import generate_summary

    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting or not meeting.transcript:
            return

        meeting.status = MeetingStatus.summarizing
        db.commit()

        try:
            summary_data = await generate_summary(meeting.transcript.full_text)

            # Обновляем существующий или создаём новый
            existing = db.query(Summary).filter(Summary.meeting_id == meeting.id).first()
            if existing:
                existing.tldr = summary_data["tldr"]
                existing.tasks = summary_data["tasks"]
                existing.topics = summary_data["topics"]
                existing.insights = summary_data["insights"]
                existing.raw_response = summary_data["raw_response"]
            else:
                summary = Summary(
                    meeting_id=meeting.id,
                    tldr=summary_data["tldr"],
                    tasks=summary_data["tasks"],
                    topics=summary_data["topics"],
                    insights=summary_data["insights"],
                    raw_response=summary_data["raw_response"],
                )
                db.add(summary)
        except Exception as e:
            meeting.error_message = f"Ошибка саммари: {e}"

        meeting.status = MeetingStatus.ready
        db.commit()
    finally:
        db.close()


@router.get("/{meeting_id}", response_class=HTMLResponse)
async def meeting_detail(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)
    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.created_at.desc()).all()

    return templates.TemplateResponse("meeting.html", {
        "request": request,
        "user": user,
        "meeting": meeting,
        "folders": folders,
    })


@router.patch("/{meeting_id}", response_class=HTMLResponse)
async def update_meeting(
    request: Request,
    meeting_id: int,
    title: str = Form(None),
    folder_id: int = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)

    if title is not None:
        meeting.title = title
    if folder_id is not None:
        # Validate folder ownership
        if folder_id:
            get_user_folder(folder_id, user, db)
        meeting.folder_id = folder_id

    db.commit()
    db.refresh(meeting)

    return templates.TemplateResponse("partials/meeting_card.html", {
        "request": request,
        "meeting": meeting,
    })


@router.delete("/{meeting_id}", response_class=HTMLResponse)
async def delete_meeting(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)

    # Удаляем файлы с диска
    if meeting.audio_path:
        meeting_dir = Path(meeting.audio_path).parent
        if meeting_dir.exists():
            shutil.rmtree(meeting_dir)

    db.delete(meeting)
    db.commit()

    return HTMLResponse("", headers={"HX-Redirect": "/"})


@router.get("/{meeting_id}/transcript", response_class=HTMLResponse)
async def meeting_transcript(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)

    return templates.TemplateResponse("partials/transcript.html", {
        "request": request,
        "transcript": meeting.transcript,
    })


@router.get("/{meeting_id}/summary", response_class=HTMLResponse)
async def meeting_summary(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)

    return templates.TemplateResponse("partials/summary.html", {
        "request": request,
        "summary": meeting.summary,
    })


@router.get("/{meeting_id}/progress")
async def meeting_progress(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    """JSON прогресс обработки встречи."""
    user = get_current_user_optional(request, db)
    if not user:
        return {"percent": 0, "stage": "unknown", "label": ""}

    meeting = db.query(Meeting).filter(
        Meeting.id == meeting_id, Meeting.user_id == user.id
    ).first()
    if not meeting:
        return {"percent": 0, "stage": "unknown", "label": ""}

    status = meeting.status.value
    stages = {
        "downloading": {"percent": 10, "label": "Скачивание"},
        "transcribing": {"percent": 45, "label": "Транскрипция"},
        "summarizing": {"percent": 80, "label": "AI Companion"},
        "ready": {"percent": 100, "label": "Готово"},
        "error": {"percent": 0, "label": "Ошибка"},
    }
    info = stages.get(status, {"percent": 0, "label": status})
    return {"percent": info["percent"], "stage": status, "label": info["label"]}


@router.get("/{meeting_id}/status", response_class=HTMLResponse)
async def meeting_status(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)

    headers = {}
    if meeting.status in (MeetingStatus.ready, MeetingStatus.error):
        headers["HX-Trigger"] = "processingComplete"

    return templates.TemplateResponse(
        "partials/status_badge.html",
        {"request": request, "meeting": meeting},
        headers=headers,
    )


@router.post("/{meeting_id}/resummarize", response_class=HTMLResponse)
async def resummarize_meeting(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    """Пересгенерировать конспект из существующего транскрипта."""
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)
    if not meeting.transcript:
        raise HTTPException(status_code=422, detail="Нет транскрипта")

    meeting.status = MeetingStatus.summarizing
    db.commit()

    asyncio.create_task(_generate_summary_for_meeting(meeting.id))

    return templates.TemplateResponse("partials/status_badge.html", {
        "request": request,
        "meeting": meeting,
    })


@router.post("/{meeting_id}/retry", response_class=HTMLResponse)
async def retry_meeting(
    request: Request,
    meeting_id: int,
    db: Session = Depends(get_db),
):
    """Повторная обработка встречи (транскрибация + саммари)."""
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    meeting = get_user_meeting(meeting_id, user, db)

    if not meeting.audio_path:
        raise HTTPException(status_code=422, detail="Нет аудиофайла для обработки")

    # Сбрасываем статус
    meeting.status = MeetingStatus.transcribing
    meeting.error_message = None
    db.commit()

    # Запуск pipeline в фоне
    from app.services.pipeline import process_meeting
    asyncio.create_task(process_meeting(meeting.id))

    return templates.TemplateResponse("partials/meeting_card.html", {
        "request": request,
        "meeting": meeting,
    })


