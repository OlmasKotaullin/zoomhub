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

    # Маскированные серверные ключи (показать что ключ есть, но не раскрывать)
    from app.config import GROQ_API_KEY, GOOGLE_AI_API_KEY, GIGACHAT_AUTH_KEY, OPENAI_API_KEY

    def mask_key(key):
        if not key: return ""
        if len(key) <= 8: return key[:2] + "***" + key[-2:]
        return key[:4] + "***" + key[-4:]

    server_keys = {
        "groq": bool(GROQ_API_KEY),
        "gemini": bool(GOOGLE_AI_API_KEY),
        "anthropic": bool(ANTHROPIC_API_KEY),
        "gigachat": bool(GIGACHAT_AUTH_KEY),
        "openai": bool(OPENAI_API_KEY),
    }
    server_keys_masked = {
        "groq": mask_key(GROQ_API_KEY) if GROQ_API_KEY else "",
        "gemini": mask_key(GOOGLE_AI_API_KEY) if GOOGLE_AI_API_KEY else "",
        "anthropic": mask_key(ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else "",
        "gigachat": mask_key(GIGACHAT_AUTH_KEY) if GIGACHAT_AUTH_KEY else "",
        "openai": mask_key(OPENAI_API_KEY) if OPENAI_API_KEY else "",
    }

    # Буквица: серверная или пользовательская сессия
    bukvitsa_user_ok = bool(getattr(user, 'tg_session', None))

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "folders": folders,
        "zoom_ok": bool(user.zoom_access_token),
        "zoom_configured": bool(ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET),
        "telegram_ok": bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and session_exists),
        "bukvitsa_username": BUKVITSA_BOT_USERNAME,
        "bukvitsa_user_ok": bukvitsa_user_ok,
        "claude_ok": bool(ANTHROPIC_API_KEY),
        "llm_provider": config_module.LLM_PROVIDER,
        "llm_ok": llm_ok,
        "ollama_model": config_module.OLLAMA_MODEL,
        "transcription_provider": config_module.TRANSCRIPTION_PROVIDER,
        "transcription_ok": trans_ok or bukvitsa_user_ok,
        "whisper_model": config_module.WHISPER_MODEL,
        "server_keys": server_keys,
        "server_keys_masked": server_keys_masked,
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


@router.post("/settings/telegram/send-code")
async def tg_send_code(
    request: Request,
    api_id: int = Form(...),
    api_hash: str = Form(...),
    phone: str = Form(...),
    bot_username: str = Form("bykvitsa"),
    db: Session = Depends(get_db),
):
    """Шаг 1: отправить код подтверждения на телефон пользователя."""
    user = get_current_user_optional(request, db)
    if not user:
        return {"status": "error", "detail": "Не авторизован"}

    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from app.services.providers.bukvitsa_provider import _pending_auth

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        return {"status": "error", "detail": str(e)}

    _pending_auth[user.id] = (client, phone, api_id, api_hash, bot_username)
    return {"status": "ok", "detail": "Код отправлен в Telegram"}


@router.post("/settings/telegram/confirm-code")
async def tg_confirm_code(
    request: Request,
    code: str = Form(...),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Шаг 2: подтвердить код (и 2FA-пароль если есть)."""
    user = get_current_user_optional(request, db)
    if not user:
        return {"status": "error", "detail": "Не авторизован"}

    from app.services.providers.bukvitsa_provider import _pending_auth

    pending = _pending_auth.get(user.id)
    if not pending:
        return {"status": "error", "detail": "Сначала отправьте код. Начните заново."}

    client, phone, api_id, api_hash, bot_username = pending

    try:
        await client.sign_in(phone, code)
    except Exception as e:
        err = str(e)
        if "Two-steps verification" in err or "password" in err.lower():
            if not password:
                return {"status": "need_password", "detail": "Введите пароль двухфакторной аутентификации"}
            try:
                await client.sign_in(password=password)
            except Exception as e2:
                await client.disconnect()
                _pending_auth.pop(user.id, None)
                return {"status": "error", "detail": str(e2)}
        else:
            await client.disconnect()
            _pending_auth.pop(user.id, None)
            return {"status": "error", "detail": err}

    session_string = client.session.save()
    await client.disconnect()
    _pending_auth.pop(user.id, None)

    user.tg_api_id = api_id
    user.tg_api_hash = api_hash
    user.tg_bot_username = bot_username.strip("@") or "bykvitsa"
    user.tg_session = session_string
    db.commit()

    return {"status": "ok", "detail": "Telegram подключён. Теперь загрузка файлов использует вашу Буквицу."}


@router.post("/settings/telegram/disconnect")
async def tg_disconnect(request: Request, db: Session = Depends(get_db)):
    """Отключить личный Telegram (удалить сессию)."""
    user = get_current_user_optional(request, db)
    if not user:
        return {"status": "error", "detail": "Не авторизован"}

    from app.services.providers.bukvitsa_provider import _user_clients
    _user_clients.pop(user.id, None)

    user.tg_api_id = None
    user.tg_api_hash = None
    user.tg_bot_username = None
    user.tg_session = None
    db.commit()
    return {"status": "ok"}


@router.post("/settings/api-keys")
async def save_api_keys(
    request: Request,
    groq_key: str = Form(""),
    gemini_key: str = Form(""),
    anthropic_key: str = Form(""),
    gigachat_key: str = Form(""),
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
    user.user_gigachat_auth_key = gigachat_key.strip() or None
    user.user_openai_api_key = openai_key.strip() or None
    db.commit()

    return {"status": "ok"}


@router.get("/settings/health/{provider_type}")
async def check_provider_health(request: Request, provider_type: str, provider: str = "", db: Session = Depends(get_db)):
    """Проверяет здоровье провайдера. ?provider=groq|gemini|claude|gigachat|openai"""
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    try:
        if provider and provider_type == "llm":
            from app.services.providers.registry import make_provider_by_name
            p = make_provider_by_name(provider)
        elif provider_type == "llm":
            from app.services.providers import get_llm_provider
            p = get_llm_provider()
        elif provider_type == "transcription":
            from app.services.providers import get_transcription_provider
            p = get_transcription_provider()
        else:
            raise HTTPException(status_code=400, detail="Тип: llm или transcription")

        ok = await p.health_check()
        return {"ok": ok, "provider": p.name}
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
