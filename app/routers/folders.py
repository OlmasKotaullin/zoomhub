from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from pathlib import Path

from app.database import get_db
from app.deps import templates, get_current_user_optional, get_user_folder
from app.models import Folder, Meeting
from app.config import (
    ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_ACCOUNT_ID,
    TELEGRAM_API_ID, TELEGRAM_API_HASH, BUKVITSA_BOT_USERNAME,
    ANTHROPIC_API_KEY, BASE_DIR,
    LLM_PROVIDER, TRANSCRIPTION_PROVIDER, OLLAMA_MODEL, WHISPER_MODEL,
)
import app.config as config_module

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.created_at.desc()).all()
    session_exists = Path(f"{BASE_DIR}/zoomhub.session").exists()

    # Health check провайдеров
    from app.services.providers import get_llm_provider, get_transcription_provider
    try:
        llm = get_llm_provider()
        llm_ok = await llm.health_check()
    except Exception:
        llm_ok = False

    try:
        trans = get_transcription_provider()
        trans_ok = await trans.health_check()
    except Exception:
        trans_ok = False

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "folders": folders,
        "zoom_ok": bool(user.zoom_access_token),
        "zoom_configured": bool(ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET),
        "telegram_ok": bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and session_exists),
        "bukvitsa_username": BUKVITSA_BOT_USERNAME,
        "claude_ok": bool(ANTHROPIC_API_KEY),
        "llm_provider": config_module.LLM_PROVIDER,
        "llm_ok": llm_ok,
        "ollama_model": config_module.OLLAMA_MODEL,
        "transcription_provider": config_module.TRANSCRIPTION_PROVIDER,
        "transcription_ok": trans_ok,
        "whisper_model": config_module.WHISPER_MODEL,
    })


@router.post("/settings/llm-provider")
async def switch_llm_provider(request: Request, provider: str = Form(...), db: Session = Depends(get_db)):
    """Переключает LLM-провайдер (claude / ollama)."""
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if provider not in ("claude", "ollama", "gemini", "groq", "auto"):
        raise HTTPException(status_code=400, detail="Неизвестный провайдер")

    config_module.LLM_PROVIDER = provider
    from app.services.providers.registry import reset_llm_provider
    reset_llm_provider()

    return {"status": "ok", "provider": provider}


@router.post("/settings/transcription-provider")
async def switch_transcription_provider(request: Request, provider: str = Form(...), db: Session = Depends(get_db)):
    """Переключает транскрипция-провайдер (bukvitsa / whisper)."""
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if provider not in ("bukvitsa", "whisper", "openai_whisper"):
        raise HTTPException(status_code=400, detail="Неизвестный провайдер")

    config_module.TRANSCRIPTION_PROVIDER = provider
    from app.services.providers.registry import reset_transcription_provider
    reset_transcription_provider()

    return {"status": "ok", "provider": provider}


@router.post("/settings/api-keys")
async def save_api_keys(
    request: Request,
    groq_key: str = Form(""),
    gemini_key: str = Form(""),
    anthropic_key: str = Form(""),
    openai_key: str = Form(""),
    db: Session = Depends(get_db),
):
    """Сохраняет пользовательские API-ключи."""
    user = get_current_user_optional(request, db)
    if not user:
        return {"status": "error", "detail": "Не авторизован"}

    user.user_groq_api_key = groq_key.strip() or None
    user.user_gemini_api_key = gemini_key.strip() or None
    user.user_anthropic_api_key = anthropic_key.strip() or None
    user.user_openai_api_key = openai_key.strip() or None
    db.commit()

    return {"status": "ok"}


@router.get("/settings/health/{provider_type}")
async def check_provider_health(request: Request, provider_type: str, db: Session = Depends(get_db)):
    """Проверяет здоровье провайдера."""
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    try:
        if provider_type == "llm":
            from app.services.providers import get_llm_provider
            provider = get_llm_provider()
        elif provider_type == "transcription":
            from app.services.providers import get_transcription_provider
            provider = get_transcription_provider()
        else:
            raise HTTPException(status_code=400, detail="Тип: llm или transcription")

        ok = await provider.health_check()
        return {"ok": ok, "provider": provider.name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.onboarding_completed:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("onboarding.html", {"request": request, "user": user})


@router.post("/onboarding/complete")
async def onboarding_complete(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    user.onboarding_completed = True
    db.commit()
    return RedirectResponse("/", status_code=302)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    from app.models import MeetingStatus, Transcript
    from sqlalchemy import func

    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.created_at.desc()).all()
    recent_meetings = (
        db.query(Meeting)
        .filter(Meeting.user_id == user.id)
        .order_by(Meeting.created_at.desc())
        .limit(20)
        .all()
    )

    # Stats
    total = db.query(func.count(Meeting.id)).filter(Meeting.user_id == user.id).scalar() or 0
    ready = db.query(func.count(Meeting.id)).filter(Meeting.user_id == user.id, Meeting.status == MeetingStatus.ready).scalar() or 0
    processing = db.query(func.count(Meeting.id)).filter(
        Meeting.user_id == user.id,
        Meeting.status.in_([MeetingStatus.downloading, MeetingStatus.transcribing, MeetingStatus.summarizing])
    ).scalar() or 0
    errors = db.query(func.count(Meeting.id)).filter(Meeting.user_id == user.id, Meeting.status == MeetingStatus.error).scalar() or 0

    # Total transcription words → hours estimate
    total_words = (
        db.query(func.sum(func.length(Transcript.full_text)))
        .join(Meeting)
        .filter(Meeting.user_id == user.id)
        .scalar()
    ) or 0
    # Rough: ~800 chars per minute of speech for Russian
    hours_transcribed = round(total_words / 800 / 60, 1)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "folders": folders,
        "recent_meetings": recent_meetings,
        "stats": {
            "total": total,
            "ready": ready,
            "processing": processing,
            "errors": errors,
            "hours": hours_transcribed,
        },
    })


@router.get("/folders", response_class=HTMLResponse)
async def list_folders(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.created_at.desc()).all()
    return templates.TemplateResponse("partials/folder_list.html", {
        "request": request,
        "user": user,
        "folders": folders,
    })


@router.post("/folders", response_class=HTMLResponse)
async def create_folder(
    request: Request,
    name: str = Form(...),
    icon: str = Form("📁"),
    keywords: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    folder = Folder(name=name, icon=icon, keywords=keywords, user_id=user.id)
    db.add(folder)
    db.commit()
    db.refresh(folder)

    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.created_at.desc()).all()
    return templates.TemplateResponse("partials/folder_list.html", {
        "request": request,
        "user": user,
        "folders": folders,
    })


@router.delete("/folders/{folder_id}", response_class=HTMLResponse)
async def delete_folder(
    request: Request,
    folder_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    folder = get_user_folder(folder_id, user, db)

    db.delete(folder)
    db.commit()

    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.created_at.desc()).all()
    return templates.TemplateResponse("partials/folder_list.html", {
        "request": request,
        "user": user,
        "folders": folders,
    })


@router.get("/folders/{folder_id}", response_class=HTMLResponse)
async def folder_detail(
    request: Request,
    folder_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    folder = get_user_folder(folder_id, user, db)

    meetings = (
        db.query(Meeting)
        .filter(Meeting.folder_id == folder_id, Meeting.user_id == user.id)
        .order_by(Meeting.created_at.desc())
        .all()
    )
    folders = db.query(Folder).filter(Folder.user_id == user.id).order_by(Folder.created_at.desc()).all()

    return templates.TemplateResponse("folder.html", {
        "request": request,
        "user": user,
        "folder": folder,
        "meetings": meetings,
        "folders": folders,
    })
