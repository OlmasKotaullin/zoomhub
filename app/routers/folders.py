from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from pathlib import Path

from app.database import get_db
from app.deps import templates
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
    folders = db.query(Folder).order_by(Folder.created_at.desc()).all()
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
        "folders": folders,
        "zoom_ok": bool(ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET and ZOOM_ACCOUNT_ID),
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
async def switch_llm_provider(request: Request, provider: str = Form(...)):
    """Переключает LLM-провайдер (claude / ollama)."""
    if provider not in ("claude", "ollama", "auto"):
        raise HTTPException(status_code=400, detail="Неизвестный провайдер")

    config_module.LLM_PROVIDER = provider
    from app.services.providers.registry import reset_llm_provider
    reset_llm_provider()

    return {"status": "ok", "provider": provider}


@router.post("/settings/transcription-provider")
async def switch_transcription_provider(request: Request, provider: str = Form(...)):
    """Переключает транскрипция-провайдер (bukvitsa / whisper)."""
    if provider not in ("bukvitsa", "whisper"):
        raise HTTPException(status_code=400, detail="Неизвестный провайдер")

    config_module.TRANSCRIPTION_PROVIDER = provider
    from app.services.providers.registry import reset_transcription_provider
    reset_transcription_provider()

    return {"status": "ok", "provider": provider}


@router.get("/settings/health/{provider_type}")
async def check_provider_health(provider_type: str):
    """Проверяет здоровье провайдера."""
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


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    folders = db.query(Folder).order_by(Folder.created_at.desc()).all()
    recent_meetings = (
        db.query(Meeting)
        .order_by(Meeting.created_at.desc())
        .limit(10)
        .all()
    )
    return templates.TemplateResponse("index.html", {
        "request": request,
        "folders": folders,
        "recent_meetings": recent_meetings,
    })


@router.get("/folders", response_class=HTMLResponse)
async def list_folders(request: Request, db: Session = Depends(get_db)):
    folders = db.query(Folder).order_by(Folder.created_at.desc()).all()
    return templates.TemplateResponse("partials/folder_list.html", {
        "request": request,
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
    folder = Folder(name=name, icon=icon, keywords=keywords)
    db.add(folder)
    db.commit()
    db.refresh(folder)

    folders = db.query(Folder).order_by(Folder.created_at.desc()).all()
    return templates.TemplateResponse("partials/folder_list.html", {
        "request": request,
        "folders": folders,
    })


@router.delete("/folders/{folder_id}", response_class=HTMLResponse)
async def delete_folder(
    request: Request,
    folder_id: int,
    db: Session = Depends(get_db),
):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Папка не найдена")

    db.delete(folder)
    db.commit()

    folders = db.query(Folder).order_by(Folder.created_at.desc()).all()
    return templates.TemplateResponse("partials/folder_list.html", {
        "request": request,
        "folders": folders,
    })


@router.get("/folders/{folder_id}", response_class=HTMLResponse)
async def folder_detail(
    request: Request,
    folder_id: int,
    db: Session = Depends(get_db),
):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Папка не найдена")

    meetings = (
        db.query(Meeting)
        .filter(Meeting.folder_id == folder_id)
        .order_by(Meeting.created_at.desc())
        .all()
    )
    folders = db.query(Folder).order_by(Folder.created_at.desc()).all()

    return templates.TemplateResponse("folder.html", {
        "request": request,
        "folder": folder,
        "meetings": meetings,
        "folders": folders,
    })
