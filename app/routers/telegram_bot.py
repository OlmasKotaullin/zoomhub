"""Telegram Bot — второй фронтенд ZoomHub.

Пользователь кидает аудио/видео/голосовое в бота →
бот транскрибирует → делает AI-саммари →
результат в Telegram + автоматически на zoomhub.ru (та же БД).

Расширяет существующий webhook из auth.py.
"""

import asyncio
from contextlib import asynccontextmanager
import io
import json
import logging
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from sqlalchemy.orm import Session

from app.config import TELEGRAM_BOT_TOKEN, RECORDINGS_DIR, APP_URL, ALLOWED_EXTENSIONS
from app.database import get_db, SessionLocal
from app.models import User, Meeting, MeetingStatus, MeetingSource

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram-bot"])

# Supported media types from Telegram
_AUDIO_TYPES = ("audio", "voice", "video", "video_note", "document")

# Max file size Telegram Bot API can download: 20 MB
_TG_MAX_DOWNLOAD = 20 * 1024 * 1024

# Pending media for protective intercept in chat mode
_pending_media: dict[str, dict] = {}

# Last command response message ID per chat (to delete on next command)
_last_cmd_msg: dict[str, int] = {}

# Telegram registration state machine
_pending_reg: dict[str, dict] = {}  # chat_id -> {"step": "name"|"email", "name": str, "invite_code": str, "ts": float}
_REG_TTL = 600  # 10 minutes timeout

# Support mode: waiting for user's support message
_pending_support: dict[str, float] = {}  # chat_id -> timestamp
_SUPPORT_TTL = 300  # 5 minutes timeout

# Semaphore: limit concurrent pipeline processing (downloads + transcription)
_pipeline_sem = asyncio.Semaphore(2)  # max 2 simultaneous recordings

# ──────────────── Persistent Reply Keyboards ────────────────

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📋 Мои записи"}, {"text": "📊 Тариф"}],
        [{"text": "🌐 Веб-кабинет"}, {"text": "❓ Помощь"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

CHAT_KEYBOARD = {
    "keyboard": [[{"text": "❌ Завершить чат"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Button text → handler mapping (checked BEFORE AI-chat)
_BUTTON_ROUTES = {
    "📋 Мои записи": "meetings",
    "📊 Тариф": "plan",
    "🌐 Веб-кабинет": "web",
    "❓ Помощь": "help",
    "❌ Завершить чат": "exit",
}


async def _tg_send_cmd(chat_id: str, text: str, **kwargs) -> dict | None:
    """Send a command response and remember its ID for cleanup on next command."""
    # Delete previous command response
    old_msg_id = _last_cmd_msg.pop(chat_id, None)
    if old_msg_id:
        try:
            await _tg_api("deleteMessage", chat_id=chat_id, message_id=old_msg_id)
        except Exception:
            pass  # message may already be deleted or too old

    result = await _tg_send(chat_id, text, **kwargs)
    if result and result.get("ok") and result.get("result", {}).get("message_id"):
        _last_cmd_msg[chat_id] = result["result"]["message_id"]
    return result

# Chat mode state: in-memory locks + DB-backed meeting_id
_chat_locks: dict[str, asyncio.Lock] = {}  # per-chat locks for race condition protection


def _get_chat_meeting_id(chat_id: str) -> int | None:
    """Get active chat meeting_id from DB (persists across restarts)."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
        return user.current_chat_meeting_id if user else None
    finally:
        db.close()


def _set_chat_meeting_id(chat_id: str, meeting_id: int | None):
    """Set/clear active chat meeting_id in DB."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
        if user:
            user.current_chat_meeting_id = meeting_id
            db.commit()
    finally:
        db.close()


def _is_in_chat(chat_id: str) -> bool:
    """Check if user is in AI chat mode."""
    return _get_chat_meeting_id(chat_id) is not None


# ──────────────── Typing indicator ────────────────

@asynccontextmanager
async def _typing_loop(chat_id: str):
    """Send typing indicator every 4 sec until context exits."""
    stop = asyncio.Event()

    async def _loop():
        while not stop.is_set():
            await _tg_api("sendChatAction", chat_id=chat_id, action="typing")
            try:
                await asyncio.wait_for(stop.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        task.cancel()


# ──────────────── Bot setup ────────────────

async def setup_bot_commands():
    """Register bot commands menu in Telegram."""
    await _tg_api("setMyCommands", commands=[
        {"command": "start", "description": "Начать работу с ZoomHub"},
        {"command": "help", "description": "Справка"},
        {"command": "plan", "description": "Тариф и лимиты"},
        {"command": "meetings", "description": "Последние записи"},
        {"command": "web", "description": "Открыть личный кабинет"},
        {"command": "exit", "description": "Выйти из AI-чата"},
    ])


# ──────────────── Telegram API helpers ────────────────

async def _tg_api(method: str, **kwargs):
    """Call Telegram Bot API method."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=kwargs)
        return resp.json()


async def _tg_send(chat_id: str, text: str, parse_mode: str = "Markdown",
                   reply_markup: dict | None = None):
    """Send message to Telegram chat."""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _tg_api("sendMessage", **payload)


async def _tg_edit(chat_id: str, message_id: int, text: str,
                   parse_mode: str = "Markdown", reply_markup: dict | None = None):
    """Edit existing Telegram message."""
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _tg_api("editMessageText", **payload)


async def _tg_send_document(chat_id: str, buf: io.BytesIO, filename: str,
                            caption: str = ""):
    """Send document to Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "Markdown"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, data=data,
                                 files={"document": (filename, buf, "text/plain")})
        return resp.json()


# Callback data pattern: action:meeting_id[:extra]
CALLBACK_PATTERN = re.compile(r"^(\w+):(\d+)(?::(.+))?$")


async def _tg_download_file(file_id: str, dest_path: str, file_size: int = 0,
                            message_id: int = 0, chat_id: str = "") -> bool:
    """Download file from Telegram by file_id.

    Files <= 20 MB: Bot API getFile (fast).
    Files > 20 MB: Telethon client (supports up to 2 GB).
    """
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)

    if file_size > _TG_MAX_DOWNLOAD:
        # Large file — use Telethon (already running on server for Bukvitsa)
        return await _tg_download_via_telethon(file_id, dest_path, file_size,
                                                message_id=message_id, chat_id=chat_id)

    # Small file — Bot API
    result = await _tg_api("getFile", file_id=file_id)
    file_info = result.get("result", {})
    tg_file_path = file_info.get("file_path")
    actual_size = file_info.get("file_size", 0)

    if not tg_file_path:
        logger.error(f"Telegram getFile failed: {result}")
        return False

    # Bot API may report larger size than initial estimate
    if actual_size > _TG_MAX_DOWNLOAD:
        return await _tg_download_via_telethon(file_id, dest_path, actual_size,
                                                message_id=message_id, chat_id=chat_id)

    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_file_path}"
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.get(download_url)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)

    size_mb = Path(dest_path).stat().st_size / (1024 * 1024)
    logger.info(f"Downloaded {size_mb:.1f} MB via Bot API -> {dest_path}")
    return True


async def _tg_download_via_telethon(file_id: str, dest_path: str, file_size: int = 0,
                                     message_id: int = 0, chat_id: str = "") -> bool:
    """Download large file via Telethon Bot Client (up to 2 GB).

    Uses InputPeerUser + get_messages to fetch the message, then download_media.
    """
    try:
        size_mb = file_size / (1024 * 1024)
        logger.info(f"Downloading {size_mb:.1f} MB via Telethon Bot (msg_id={message_id}, chat={chat_id})...")

        if not message_id or not chat_id:
            logger.error("message_id and chat_id required for Telethon download")
            return False

        bot_client = await _get_bot_client()
        if not bot_client:
            logger.error("Could not create Telethon bot client")
            return False

        # For bots, we need to use InputPeerUser with the user's access_hash
        # get_input_entity resolves chat_id to proper InputPeer
        try:
            peer = await bot_client.get_input_entity(int(chat_id))
        except Exception as e:
            logger.warning(f"get_input_entity failed: {e}, trying raw int")
            peer = int(chat_id)

        messages = await bot_client.get_messages(peer, ids=message_id)
        if not messages:
            logger.error(f"Message {message_id} not found for peer {chat_id}")
            return False

        msg = messages if not isinstance(messages, list) else messages[0] if messages else None
        if not msg or not msg.media:
            logger.error(f"Message {message_id} has no media")
            return False

        # Download media via Telethon MTProto (supports up to 2 GB)
        logger.info(f"Starting Telethon download of {size_mb:.1f} MB...")
        downloaded = await bot_client.download_media(msg, file=dest_path)
        if downloaded and Path(str(downloaded)).exists():
            actual_size = Path(str(downloaded)).stat().st_size / (1024 * 1024)
            logger.info(f"Downloaded {actual_size:.1f} MB via Telethon Bot -> {downloaded}")
            if str(downloaded) != dest_path:
                import shutil
                shutil.move(str(downloaded), dest_path)
            return True

        logger.error("Telethon bot download returned None")
        return False

    except Exception as e:
        logger.error(f"Telethon bot download error: {e}", exc_info=True)
        return False


# Singleton Telethon bot client
_bot_client = None
_bot_client_lock = asyncio.Lock()


async def _get_bot_client():
    """Get or create Telethon client authenticated as the bot."""
    global _bot_client

    async with _bot_client_lock:
        if _bot_client and _bot_client.is_connected():
            return _bot_client

        from app.config import TELEGRAM_API_ID, TELEGRAM_API_HASH
        from telethon import TelegramClient
        from telethon.sessions import MemorySession

        if not TELEGRAM_API_ID or not TELEGRAM_API_HASH or not TELEGRAM_BOT_TOKEN:
            logger.error("TELEGRAM_API_ID/HASH/BOT_TOKEN not configured")
            return None

        client = TelegramClient(MemorySession(), TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.start(bot_token=TELEGRAM_BOT_TOKEN)
        _bot_client = client
        logger.info("Telethon bot client connected")
        return _bot_client


# ──────────────── Media extraction ────────────────

def _extract_media(message: dict) -> tuple[str | None, str, int]:
    """Extract file_id, filename, file_size from Telegram message.

    Returns (file_id, filename, file_size) or (None, "", 0).
    """
    # Voice message (ogg opus)
    if "voice" in message:
        v = message["voice"]
        return v["file_id"], "voice.ogg", v.get("file_size", 0)

    # Audio file (mp3, m4a, etc.)
    if "audio" in message:
        a = message["audio"]
        name = a.get("file_name", "audio.mp3")
        return a["file_id"], name, a.get("file_size", 0)

    # Video
    if "video" in message:
        v = message["video"]
        name = v.get("file_name", "video.mp4")
        return v["file_id"], name, v.get("file_size", 0)

    # Video note (round video)
    if "video_note" in message:
        vn = message["video_note"]
        return vn["file_id"], "videonote.mp4", vn.get("file_size", 0)

    # Document (check if audio/video by mime)
    if "document" in message:
        d = message["document"]
        mime = d.get("mime_type", "")
        if mime.startswith("audio/") or mime.startswith("video/"):
            name = d.get("file_name", "document.mp4")
            return d["file_id"], name, d.get("file_size", 0)

    return None, "", 0


# ──────────────── User lookup ────────────────

def _find_user_by_chat_id(chat_id: str, db: Session) -> User | None:
    """Find user linked to this Telegram chat_id."""
    return db.query(User).filter(User.telegram_chat_id == chat_id).first()


def _reset_month_if_needed(user: User, db: Session):
    """Reset ALL monthly counters if new month started."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    if (not user.usage_month_start
            or user.usage_month_start.month != now.month
            or user.usage_month_start.year != now.year):
        user.usage_seconds_month = 0
        user.chat_questions_month = 0
        user.usage_month_start = now
        db.commit()


def _check_usage_limit(user: User, db: Session) -> tuple[bool, float, float]:
    """Check if user has remaining transcription hours. Returns (ok, used_hours, limit_hours)."""
    _reset_month_if_needed(user, db)

    limit_hours = user.plan_hours_limit or 4
    used_hours = (user.usage_seconds_month or 0) / 3600
    ok = used_hours < limit_hours
    return ok, round(used_hours, 1), limit_hours


_CHAT_QUESTIONS_FREE = 3   # per meeting for free plan
_CHAT_QUESTIONS_PAID = None  # unlimited for paid plans


def _user_has_own_keys(user: User) -> bool:
    """Check if user has their own LLM API keys configured."""
    from app.services.providers.registry import get_user_keys
    return bool(get_user_keys(user))


def _check_chat_limit(meeting: Meeting, user: User) -> tuple[bool, int, int | None, str | None]:
    """Check AI chat question limit per meeting. Returns (ok, used, limit, warning_text).

    limit=None means unlimited. Bypass for: paid plans, users with own API keys, templates.
    """
    if user.plan in ("start", "pro") or _user_has_own_keys(user):
        return True, meeting.chat_questions_used or 0, None, None

    used = meeting.chat_questions_used or 0
    limit = _CHAT_QUESTIONS_FREE

    remaining = limit - used
    if remaining <= 0:
        return False, used, limit, None

    warning = None
    if remaining == 1:
        warning = f"Остался 1 вопрос из {limit} по этой записи."
    return True, used, limit, warning


def _increment_chat_usage(meeting: Meeting, db: Session):
    """Increment per-meeting chat question counter AFTER successful AI response."""
    meeting.chat_questions_used = (meeting.chat_questions_used or 0) + 1
    db.commit()


# ──────────────── Webhook handler ────────────────

@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    """Handle all Telegram bot updates."""
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": True}

    body = await request.json()

    # Handle callback_query (inline button presses)
    callback = body.get("callback_query")
    if callback:
        asyncio.create_task(_handle_callback(callback))
        return {"ok": True}

    message = body.get("message", {})

    if not message:
        return {"ok": True}

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "")

    if not chat_id:
        return {"ok": True}

    in_chat = _is_in_chat(chat_id)

    # Priority 1: Commands (any mode)
    if text.startswith("/start"):
        _set_chat_meeting_id(chat_id, None)
        await _handle_start(chat_id, text)
        return {"ok": True}

    if text.startswith("/help"):
        await _handle_help(chat_id)
        return {"ok": True}

    if text == "/exit":
        if in_chat:
            _set_chat_meeting_id(chat_id, None)
            await _tg_send_cmd(chat_id, "Чат завершён. Отправьте аудио для новой транскрипции.",
                               reply_markup=MAIN_KEYBOARD)
        else:
            await _tg_send_cmd(chat_id, "Вы не в AI-чате. Отправьте аудио для транскрипции.")
        return {"ok": True}

    if text.startswith("/plan"):
        await _handle_plan(chat_id)
        return {"ok": True}

    if text.startswith("/web"):
        await _handle_web(chat_id)
        return {"ok": True}

    if text.startswith("/invite"):
        await _handle_invite(chat_id, text)
        return {"ok": True}

    if text.startswith("/meetings"):
        await _handle_meetings(chat_id)
        return {"ok": True}

    # Priority 1.5: Reply keyboard buttons (persistent bottom keyboard)
    btn_route = _BUTTON_ROUTES.get(text)
    if btn_route:
        if btn_route == "exit":
            if in_chat:
                _set_chat_meeting_id(chat_id, None)
                await _tg_send_cmd(chat_id, "Чат завершён. Отправьте аудио для новой транскрипции.",
                                   reply_markup=MAIN_KEYBOARD)
            else:
                await _tg_send_cmd(chat_id, "Вы не в AI-чате. Отправьте аудио для транскрипции.")
        elif btn_route == "meetings":
            await _handle_meetings(chat_id)
        elif btn_route == "plan":
            await _handle_plan(chat_id)
        elif btn_route == "web":
            await _handle_web(chat_id)
        elif btn_route == "help":
            await _handle_help(chat_id)
        return {"ok": True}

    # Priority 1.8: Voice in support mode → transcribe and create ticket
    file_id, filename, file_size = _extract_media(message)
    message_id = message.get("message_id", 0)
    if file_id and "voice" in message and chat_id in _pending_support:
        import time
        ts = _pending_support.pop(chat_id)
        if time.time() - ts < _SUPPORT_TTL:
            asyncio.create_task(_handle_voice_support(chat_id, file_id, file_size, message_id))
            return {"ok": True}

    # Priority 2: Media handling (depends on chat mode)
    if file_id:
        if in_chat and "voice" in message:
            # Voice in chat mode → transcribe and send as AI question
            asyncio.create_task(_handle_voice_question(chat_id, file_id, file_size, message_id))
            return {"ok": True}
        if in_chat:
            # Non-voice media in chat mode → ask user what to do (protective intercept)
            # Store pending file info for callback
            _pending_media[chat_id] = {"file_id": file_id, "filename": filename,
                                        "file_size": file_size, "message_id": message_id}
            await _tg_send(chat_id, "Вы отправили файл. Что сделать?",
                           reply_markup={"inline_keyboard": [
                               [{"text": "📝 Новая транскрипция", "callback_data": "media_new"}],
                               [{"text": "↩️ Остаться в чате", "callback_data": "media_stay"}],
                           ]})
            return {"ok": True}
        # Not in chat → new recording
        asyncio.create_task(_handle_media(chat_id, file_id, filename, file_size, message_id=message_id))
        return {"ok": True}

    # Priority 3: Text in chat mode → AI question
    if text and in_chat:
        asyncio.create_task(_handle_chat_message(chat_id, text))
        return {"ok": True}

    # Priority 3.5: Support message (user clicked "Написать в поддержку")
    if text and chat_id in _pending_support:
        import time
        ts = _pending_support.pop(chat_id)
        if time.time() - ts < _SUPPORT_TTL:
            asyncio.create_task(_create_support_ticket(chat_id, text))
            return {"ok": True}

    # Priority 4: Telegram registration flow
    if text and chat_id in _pending_reg:
        import time
        reg = _pending_reg[chat_id]
        # TTL check
        if time.time() - reg.get("ts", 0) > _REG_TTL:
            _pending_reg.pop(chat_id, None)
            await _tg_send(chat_id, "Время регистрации истекло. Отправьте ссылку заново.")
            return {"ok": True}

        if reg["step"] == "name":
            reg["name"] = text.strip()[:100]
            reg["step"] = "email"
            await _tg_send(chat_id, f"{reg['name']}, укажите email (для личного кабинета на сайте):")
            return {"ok": True}

        elif reg["step"] == "email":
            email = text.strip().lower()
            # Basic email validation
            if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
                await _tg_send(chat_id, "Некорректный email. Попробуйте ещё раз:")
                return {"ok": True}

            # Check duplicate
            db = SessionLocal()
            try:
                existing = db.query(User).filter(User.email == email).first()
                if existing:
                    await _tg_send(chat_id, "Этот email уже зарегистрирован. Введите другой:")
                    return {"ok": True}

                # Create user
                import secrets
                from app.auth import hash_password
                from app.models import InviteCode

                user = User(
                    name=reg["name"],
                    email=email,
                    hashed_password=hash_password(secrets.token_urlsafe(32)),
                    telegram_chat_id=chat_id,
                    notify_telegram=True,
                    onboarding_completed=True,
                )
                db.add(user)
                db.flush()

                # Use invite code
                invite_code = reg.get("invite_code", "")
                if invite_code:
                    ic = db.query(InviteCode).filter(InviteCode.code == invite_code).first()
                    if ic:
                        ic.used_count = (ic.used_count or 0) + 1
                        ic.used_by_id = user.id
                        user.invite_code_id = ic.id

                # Generate 2 personal invite codes
                import secrets as _sec
                for _ in range(2):
                    code = f"ZH-{_sec.token_hex(3).upper()}"
                    db.add(InviteCode(code=code, owner_id=user.id))

                db.commit()
                _pending_reg.pop(chat_id, None)

                await _tg_send(
                    chat_id,
                    f"✅ *Готово, {reg['name']}!*\n\n"
                    f"Аккаунт создан. Бесплатно: 4 ч транскрипции в месяц.\n\n"
                    f"Отправьте аудио или видео — конспект будет через 2-3 мин.",
                    reply_markup=MAIN_KEYBOARD,
                )
            finally:
                db.close()
            return {"ok": True}

    # Priority 5: Text without chat mode → hint
    if text and not text.startswith("/"):
        await _tg_send(
            chat_id,
            "Отправьте аудио или видео для транскрипции.\n"
            "Поддерживаемые форматы: MP3, M4A, MP4, WAV, OGG, WebM.",
            reply_markup=MAIN_KEYBOARD,
        )

    return {"ok": True}


# ──────────────── Command handlers ────────────────

async def _handle_start(chat_id: str, text: str):
    """Handle /start [token|invite_code] — link or register via Telegram."""
    import time
    from app.auth import decode_token
    from app.models import InviteCode

    parts = text.split(maxsplit=1)
    param = parts[1].strip() if len(parts) > 1 else ""

    db = SessionLocal()
    try:
        # Check if already linked
        existing_user = _find_user_by_chat_id(chat_id, db)

        if param.startswith("ZH-"):
            # Invite code → start Telegram registration
            if existing_user:
                await _tg_send(
                    chat_id,
                    f"*Привет, {existing_user.name}!*\n\n"
                    f"Ваш аккаунт уже подключён.\n"
                    f"Отправьте аудио или видео для транскрипции.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            ic = db.query(InviteCode).filter(
                InviteCode.code == param,
                InviteCode.is_active == True,
            ).first()
            if not ic or (ic.max_uses and ic.used_count >= ic.max_uses):
                await _tg_send(chat_id,
                    "Инвайт-код недействителен или уже использован.\n"
                    "Запросите новый у того, кто вас пригласил.")
                return

            # Start registration flow
            _pending_reg[chat_id] = {
                "step": "name",
                "invite_code": param,
                "ts": time.time(),
            }
            await _tg_send(chat_id,
                "*ZoomHub* — рабочая память ваших встреч 🎙\n\n"
                "Для начала — как вас зовут?")
            return

        if param:
            # JWT token → link existing account
            user_id = decode_token(param)
            if user_id:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    user.telegram_chat_id = chat_id
                    user.notify_telegram = True
                    db.commit()
                    await _tg_send(
                        chat_id,
                        f"*Привет, {user.name}!* 👋\n\n"
                        f"Telegram подключён к ZoomHub.\n"
                        f"Отправьте аудио или видео — конспект за 2-3 мин.\n\n"
                        f"Бесплатно {user.plan_hours_limit or 4} ч/мес.",
                        reply_markup=MAIN_KEYBOARD,
                    )
                    return

            await _tg_send(
                chat_id,
                "Ссылка привязки устарела.\nПолучите новую в настройках ZoomHub:",
                reply_markup={"inline_keyboard": [[{
                    "text": "Открыть настройки",
                    "url": f"{APP_URL}/settings"
                }]]}
            )
        else:
            # No param — already linked or new user
            if existing_user:
                await _tg_send(
                    chat_id,
                    f"*Привет, {existing_user.name}!*\n\n"
                    f"Ваш аккаунт уже подключён.\n"
                    f"Отправьте аудио или видео для транскрипции.",
                    reply_markup=MAIN_KEYBOARD,
                )
            else:
                await _tg_send(
                    chat_id,
                    "*ZoomHub* — рабочая память ваших встреч\n\n"
                    "Сейчас идёт закрытое тестирование.\n"
                    "Получите инвайт-ссылку у того, кто вас пригласил.",
                    reply_markup={"inline_keyboard": [
                        [{"text": "У меня есть аккаунт", "url": f"{APP_URL}/settings"}],
                    ]}
                )
    finally:
        db.close()


async def _handle_voice_support(chat_id: str, file_id: str, file_size: int, message_id: int):
    """Transcribe voice message and create support ticket from text."""
    try:
        async with _typing_loop(chat_id):
            text = await _transcribe_voice_groq(file_id)

        if not text:
            await _tg_send(chat_id, "Не удалось распознать голосовое. Напишите текстом.")
            return

        await _tg_send(chat_id, f"🎤 _{text}_", parse_mode="Markdown")
        await _create_support_ticket(chat_id, text)

    except Exception as e:
        logger.error(f"Voice support error: {e}", exc_info=True)
        await _tg_send(chat_id, "Ошибка. Попробуйте написать текстом.")


async def _create_support_ticket(chat_id: str, text: str):
    """Create support ticket and notify admins."""
    from app.models import SupportTicket
    db = SessionLocal()
    try:
        user = _find_user_by_chat_id(chat_id, db)
        if not user:
            await _tg_send(chat_id, "Аккаунт не найден. Напишите /start для подключения.")
            return

        ticket = SupportTicket(
            user_id=user.id,
            subject="Telegram",
            message=text[:2000],
            category="support",
            status="new",
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)

        await _tg_send(
            chat_id,
            f"✅ *Обращение #{ticket.id} создано*\n\n"
            f"Ваш вопрос получен, ответим в ближайшее время.",
            reply_markup=MAIN_KEYBOARD,
        )

        # Notify all admins via Telegram
        admins = db.query(User).filter(User.is_admin == True).all()
        for admin in admins:
            if admin.telegram_chat_id:
                plan = user.plan or "free"
                await _tg_send(
                    admin.telegram_chat_id,
                    f"🔔 *Тикет #{ticket.id}*\n"
                    f"От: {user.name} ({user.email})\n"
                    f"Тариф: {plan}\n\n"
                    f"{text[:500]}",
                    reply_markup={"inline_keyboard": [[
                        {"text": "📋 Открыть тикеты", "url": f"{APP_URL}/admin/tickets"},
                    ]]},
                )
    except Exception as e:
        logger.error(f"Support ticket error: {e}", exc_info=True)
        await _tg_send(chat_id, "Не удалось отправить. Попробуйте позже.")
    finally:
        db.close()


async def _handle_help(chat_id: str):
    """Send help message."""
    await _tg_send_cmd(
        chat_id,
        "Как вам помочь?",
        reply_markup={"inline_keyboard": [
            [{"text": "📖 Справка — как пользоваться", "callback_data": "help:faq"}],
            [{"text": "✉️ Написать в поддержку", "callback_data": "help:support"}],
        ]},
    )


async def _handle_plan(chat_id: str):
    """Show user's plan, usage and limits."""
    db = SessionLocal()
    try:
        user = _find_user_by_chat_id(chat_id, db)
        if not user:
            await _tg_send(
                chat_id,
                "Аккаунт не подключён.\n"
                f"Подключите Telegram в настройках: {APP_URL}/settings"
            )
            return

        _reset_month_if_needed(user, db)

        plan_names = {"free": "Free", "start": "Start", "pro": "Pro"}
        plan_name = plan_names.get(user.plan, user.plan or "Free")

        limit_hours = user.plan_hours_limit or 4
        used_hours = round((user.usage_seconds_month or 0) / 3600, 1)
        remaining = round(limit_hours - used_hours, 1)

        msg = f"📊 *Ваш тариф: {plan_name}*\n\n"
        msg += f"*Транскрипция:* {used_hours} из {limit_hours} ч "
        msg += f"(осталось {max(0, remaining)} ч)\n"

        if user.plan == "free":
            msg += f"*AI-чат:* 3 вопроса на каждую запись\n"
            msg += f"*Шаблоны:* безлимит (Протокол, Задачи)\n"
            msg += f"\n💡 Тариф Start (499 ₽/мес) — 30 ч + безлимитный AI-чат"
        else:
            msg += f"*AI-чат:* безлимит\n"
            msg += f"*Шаблоны:* безлимит\n"

        await _tg_send_cmd(chat_id, msg)
    finally:
        db.close()


async def _handle_invite(chat_id: str, text: str):
    """Admin-only: create invite codes. Usage: /invite 5"""
    db = SessionLocal()
    try:
        user = _find_user_by_chat_id(chat_id, db)
        if not user or not user.is_admin:
            await _tg_send(chat_id, "Эта команда доступна только администратору.")
            return

        import secrets as _sec
        from app.models import InviteCode

        # Parse count: /invite 5 → 5 codes, /invite → 1 code
        parts = text.split()
        count = min(int(parts[1]), 20) if len(parts) > 1 and parts[1].isdigit() else 1

        codes = []
        for _ in range(count):
            code = f"ZH-{_sec.token_hex(3).upper()}"
            db.add(InviteCode(code=code, max_uses=1, is_active=True, owner_id=user.id))
            codes.append(code)
        db.commit()

        lines = [f"🔑 *{count} инвайт-ссылок:*\n"]
        for c in codes:
            lines.append(f"`https://t.me/ZoomHub_notify_bot?start={c}`")

        await _tg_send_cmd(chat_id, "\n".join(lines))
    finally:
        db.close()


async def _handle_web(chat_id: str):
    """Generate magic-link to web dashboard (no password needed)."""
    db = SessionLocal()
    try:
        user = _find_user_by_chat_id(chat_id, db)
        if not user:
            await _tg_send(chat_id, "Аккаунт не подключён.")
            return
        from app.auth import create_token
        token = create_token(user.id, expires_hours=1)  # short-lived
        await _tg_send_cmd(
            chat_id,
            "Откройте личный кабинет (ссылка действует 1 час):",
            reply_markup={"inline_keyboard": [[{
                "text": "🌐 Открыть ZoomHub",
                "url": f"{APP_URL}/auth/magic?token={token}"
            }]]}
        )
    finally:
        db.close()


async def _handle_meetings(chat_id: str):
    """Show last 5 meetings with AI-chat buttons."""
    db = SessionLocal()
    try:
        user = _find_user_by_chat_id(chat_id, db)
        if not user:
            await _tg_send(
                chat_id,
                "Аккаунт не подключён.\n"
                f"Подключите Telegram в настройках: {APP_URL}/settings"
            )
            return

        meetings = (
            db.query(Meeting)
            .filter(Meeting.user_id == user.id, Meeting.status == MeetingStatus.ready)
            .order_by(Meeting.created_at.desc())
            .limit(5)
            .all()
        )

        if not meetings:
            await _tg_send(chat_id, "У вас пока нет записей. Отправьте аудио или видео.")
            return

        msg = "📁 *Последние записи:*\n"
        buttons = []
        for i, m in enumerate(meetings, 1):
            title = m.title or "Запись"
            date_str = m.date.strftime("%d.%m") if m.date else ""
            dur = f", {m.duration_seconds // 60} мин" if m.duration_seconds else ""
            # Compact: title + date + duration in button text
            btn_label = f"{title[:25]} · {date_str}{dur}"
            msg += f"\n{i}. {title} — {date_str}{dur}"
            buttons.append([{"text": btn_label, "callback_data": f"chat:{m.id}"}])

        await _tg_send_cmd(chat_id, msg, reply_markup={"inline_keyboard": buttons})
    finally:
        db.close()


# ──────────────── Media processing ────────────────

async def _handle_media(chat_id: str, file_id: str, filename: str, file_size: int, message_id: int = 0):
    """Download media from Telegram, create Meeting, run pipeline."""
    # Queue if too many concurrent downloads
    if _pipeline_sem.locked():
        queue_msg = await _tg_send(chat_id,
            "⏳ В очереди на обработку...\n"
            "Сейчас обрабатываются другие записи. Ваша начнётся автоматически.")
        async with _pipeline_sem:
            # Delete queue message when slot opens
            if queue_msg and queue_msg.get("ok"):
                try:
                    await _tg_api("deleteMessage", chat_id=chat_id,
                                  message_id=queue_msg["result"]["message_id"])
                except Exception:
                    pass
            await _handle_media_inner(chat_id, file_id, filename, file_size, message_id)
    else:
        async with _pipeline_sem:
            await _handle_media_inner(chat_id, file_id, filename, file_size, message_id)


async def _handle_media_inner(chat_id: str, file_id: str, filename: str, file_size: int, message_id: int = 0):
    """Inner media handler — runs inside semaphore."""
    db = SessionLocal()
    try:
        # Find user
        user = _find_user_by_chat_id(chat_id, db)
        if not user:
            await _tg_send(
                chat_id,
                "Аккаунт не подключён.\n"
                f"Подключите Telegram в настройках: {APP_URL}/settings"
            )
            return

        # Check usage limit
        ok, used, limit = _check_usage_limit(user, db)
        if not ok:
            await _tg_send(
                chat_id,
                f"⚠️ Лимит исчерпан: {used:.1f} из {limit:.0f} ч/мес\n\n"
                f"Тариф *{user.plan or 'free'}* — {limit:.0f} ч/мес.\n"
                f"Для увеличения лимита — тариф Старт (30ч) за 499 ₽/мес.",
                reply_markup={"inline_keyboard": [[{
                    "text": "Посмотреть тарифы",
                    "url": f"{APP_URL}/settings#billing"
                }]]}
            )
            return

        # Check file size — hard limit 2 GB (Telegram max)
        if file_size > 2 * 1024 * 1024 * 1024:
            await _tg_send(
                chat_id,
                f"Файл слишком большой ({file_size / 1024 / 1024:.0f} МБ, лимит 2 ГБ).\n"
                f"Загрузите через сайт: {APP_URL}",
                reply_markup={"inline_keyboard": [[{
                    "text": "Загрузить на сайте",
                    "url": f"{APP_URL}/meetings/upload"
                }]]}
            )
            return

        # Progress message (will be updated via editMessage)
        size_mb = file_size / (1024 * 1024) if file_size else 0
        size_info = f" ({size_mb:.0f} МБ)" if size_mb > 5 else ""
        progress_text = (
            f"⏳ Обрабатываю запись{size_info}...\n\n"
            f"— Скачивание\n"
            f"   Транскрипция\n"
            f"   Конспект"
        )
        resp = await _tg_send(chat_id, progress_text)
        progress_msg_id = resp.get("result", {}).get("message_id", 0)

        # Create meeting
        ext = Path(filename).suffix.lower() or ".ogg"
        if ext not in ALLOWED_EXTENSIONS:
            ext = ".ogg"

        title = Path(filename).stem or "Telegram"
        if title in ("voice", "audio", "video", "videonote", "document"):
            from datetime import datetime
            title = f"Запись {datetime.now().strftime('%d.%m.%Y %H:%M')}"

        meeting = Meeting(
            user_id=user.id,
            title=title,
            source=MeetingSource.telegram,
            status=MeetingStatus.transcribing,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)

        # Download file
        meeting_dir = RECORDINGS_DIR / str(meeting.id)
        meeting_dir.mkdir(parents=True, exist_ok=True)
        file_path = meeting_dir / f"original{ext}"

        success = await _tg_download_file(file_id, str(file_path), file_size=file_size,
                                          message_id=message_id, chat_id=chat_id)
        if not success:
            meeting.status = MeetingStatus.error
            meeting.error_message = "Не удалось скачать файл из Telegram"
            db.commit()
            if progress_msg_id:
                await _tg_api("deleteMessage", chat_id=chat_id, message_id=progress_msg_id)
            await _tg_send(chat_id, "Не удалось скачать файл. Попробуйте ещё раз или загрузите через сайт.")
            return

        meeting.audio_path = str(file_path)

        # Check minimum duration (reject files < 5 sec)
        try:
            import subprocess
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)],
                capture_output=True, text=True, timeout=10
            )
            duration = float(probe.stdout.strip()) if probe.returncode == 0 else 0
            if 0 < duration < 5:
                meeting.status = MeetingStatus.error
                meeting.error_message = "Файл слишком короткий"
                db.commit()
                if progress_msg_id:
                    await _tg_api("deleteMessage", chat_id=chat_id, message_id=progress_msg_id)
                await _tg_send(chat_id, "Файл слишком короткий (менее 5 секунд). Отправьте запись длиннее.")
                return
        except Exception:
            pass  # ffprobe failed — continue anyway

        db.commit()

        # Update progress: download done
        if progress_msg_id:
            await _tg_edit(chat_id, progress_msg_id,
                           "⏳ Обрабатываю запись...\n\n"
                           "✓ Скачано\n"
                           "— Транскрипция...\n"
                           "   Конспект")

        logger.info(f"Telegram upload: meeting {meeting.id} from user {user.id} ({filename})")

        # Run pipeline in background
        asyncio.create_task(_run_pipeline_and_notify(chat_id, meeting.id, progress_msg_id))

    except Exception as e:
        logger.error(f"Telegram media handler error: {e}", exc_info=True)
        await _tg_send(chat_id, "Не удалось обработать файл. Попробуйте отправить ещё раз.")
    finally:
        db.close()


async def _run_pipeline_and_notify(chat_id: str, meeting_id: int, progress_msg_id: int = 0):
    """Run pipeline in background, then send result to Telegram."""
    try:
        from app.services.pipeline import process_meeting
        await process_meeting(meeting_id)

        # Update progress: transcription done, generating summary
        if progress_msg_id:
            await _tg_edit(chat_id, progress_msg_id,
                           "⏳ Обрабатываю запись...\n\n"
                           "✓ Скачано\n"
                           "✓ Транскрипт готов\n"
                           "— Генерирую конспект...")

        # Smart title: replace generic "Запись DD.MM" with first topic or TLDR
        try:
            sdb = SessionLocal()
            m = sdb.query(Meeting).filter(Meeting.id == meeting_id).first()
            if m and m.title and m.title.startswith("Запись") and m.summary:
                new_title = None
                if m.summary.topics:
                    t = m.summary.topics[0]
                    new_title = (t.get("topic", "") if isinstance(t, dict) else str(t))
                if not new_title and m.summary.tldr:
                    new_title = m.summary.tldr
                if new_title:
                    m.title = new_title[:80].rstrip(".")
                    sdb.commit()
            sdb.close()
        except Exception:
            pass

        await _send_result(chat_id, meeting_id, progress_msg_id)
    except Exception as e:
        logger.error(f"Pipeline error for meeting {meeting_id}: {e}", exc_info=True)
        if progress_msg_id:
            await _tg_api("deleteMessage", chat_id=chat_id, message_id=progress_msg_id)
        await _tg_send(chat_id, "Произошла ошибка при обработке записи. Попробуйте отправить файл ещё раз.")


async def _send_result(chat_id: str, meeting_id: int, progress_msg_id: int = 0):
    """Send formatted transcription result back to Telegram."""
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            return

        # Delete progress message
        if progress_msg_id:
            await _tg_api("deleteMessage", chat_id=chat_id, message_id=progress_msg_id)

        if meeting.status == MeetingStatus.error:
            await _tg_send(chat_id, f"Ошибка: {meeting.error_message or 'Неизвестная ошибка'}")
            return

        parts = ["✅ *Встреча обработана*\n"]
        parts.append(f"*{meeting.title}*")

        # Metrics line
        meta = []
        if meeting.duration_seconds:
            meta.append(f"⏱ {meeting.duration_seconds // 60} мин")
        if meeting.transcript and meeting.transcript.full_text:
            chars = len(meeting.transcript.full_text)
            meta.append(f"{chars:,} символов".replace(",", " "))
        if meeting.summary and meeting.summary.tasks:
            meta.append(f"{len(meeting.summary.tasks)} задач")
        if meta:
            parts.append(" | ".join(meta))

        # TLDR
        if meeting.summary and meeting.summary.tldr:
            parts.append(f"\n{meeting.summary.tldr}")

        # Tasks
        if meeting.summary and meeting.summary.tasks:
            parts.append("\n*Задачи:*")
            for t in meeting.summary.tasks[:5]:
                task_text = t.get("task", "") if isinstance(t, dict) else str(t)
                assignee = t.get("assignee", "") if isinstance(t, dict) else ""
                line = f"  • {task_text}"
                if assignee:
                    line += f" — {assignee}"
                parts.append(line)
            if len(meeting.summary.tasks) > 5:
                parts.append(f"  _...и ещё {len(meeting.summary.tasks) - 5}_")

        # Topics
        if meeting.summary and meeting.summary.topics:
            topics_text = ", ".join(
                t.get("topic", "") if isinstance(t, dict) else str(t)
                for t in meeting.summary.topics[:5]
            )
            if topics_text:
                parts.append(f"\n*Темы:* {topics_text}")

        # Usage info
        user = db.query(User).filter(User.id == meeting.user_id).first()
        if user:
            _, used, limit = _check_usage_limit(user, db)
            remaining = max(0, limit - used)
            parts.append(f"\n📊 Осталось: {remaining:.1f} из {limit:.0f} ч/мес")

        # Branding
        parts.append("\n──────────────")
        parts.append("Создано в *ZoomHub*")

        message = "\n".join(parts)

        if len(message) > 3800:
            message = message[:3750] + "\n\n_...полный текст на сайте_"

        # Inline buttons: 2 rows
        mid = meeting.id
        keyboard = {"inline_keyboard": [
            [
                {"text": "📄 Транскрипт .txt", "callback_data": f"dl:{mid}:txt"},
                {"text": "💬 AI-чат", "callback_data": f"chat:{mid}"},
            ],
            [
                {"text": "🌐 Открыть на сайте", "url": f"{APP_URL}/meetings/{mid}"},
            ],
        ]}

        await _tg_send(chat_id, message, reply_markup=keyboard)

    finally:
        db.close()


# ──────────────── Callback query handler ────────────────

async def _handle_callback(callback: dict):
    """Route inline button presses."""
    cb_id = callback["id"]
    chat_id = str(callback["message"]["chat"]["id"])
    data = callback.get("data", "")

    # Acknowledge immediately (removes loading spinner)
    await _tg_api("answerCallbackQuery", callback_query_id=cb_id)

    # Handle media intercept callbacks (no meeting_id in pattern)
    if data == "media_new":
        pending = _pending_media.pop(chat_id, None)
        if pending:
            _set_chat_meeting_id(chat_id, None)
            asyncio.create_task(_handle_media(
                chat_id, pending["file_id"], pending["filename"],
                pending["file_size"], message_id=pending["message_id"]))
        return
    if data == "media_stay":
        _pending_media.pop(chat_id, None)
        await _tg_send(chat_id, "Продолжаем. Задайте вопрос по записи.")
        return

    if data == "help:faq":
        await _tg_send(
            chat_id,
            "*Как пользоваться ZoomHub*\n\n"
            "*1.* Отправьте аудио или видео — через 2-3 мин получите конспект с задачами.\n\n"
            "*2.* Кнопки внизу:\n"
            "📋 Мои записи — список встреч\n"
            "📊 Тариф — лимиты и часы\n"
            "🌐 Веб-кабинет — полный интерфейс\n\n"
            "*Форматы:* MP3, M4A, MP4, WAV, OGG, WebM (до 2 ГБ)\n"
            "*Голосовые:* в AI-чате можно задавать вопросы голосом 🎤",
        )
        return

    if data == "help:support":
        import time
        _pending_support[chat_id] = time.time()
        await _tg_send(
            chat_id,
            "✉️ *Поддержка ZoomHub*\n\n"
            "Опишите вопрос или проблему одним сообщением — я передам команде.\n\n"
            "_Отправьте текст прямо сейчас:_",
        )
        return

    match = CALLBACK_PATTERN.match(data)
    if not match:
        return

    action, meeting_id, extra = match.group(1), int(match.group(2)), match.group(3)

    db = SessionLocal()
    try:
        # Verify user owns this meeting
        user = _find_user_by_chat_id(chat_id, db)
        if not user:
            await _tg_send(chat_id, "Аккаунт не подключён.")
            return

        meeting = db.query(Meeting).filter(
            Meeting.id == meeting_id,
            Meeting.user_id == user.id
        ).first()
        if not meeting:
            await _tg_send(chat_id, "Встреча не найдена.")
            return

        if action == "dl":
            await _handle_download(chat_id, meeting, extra)
        elif action == "chat":
            await _enter_chat_mode(chat_id, meeting, user)
        elif action == "tpl":
            # Template button pressed
            template_key = extra
            tpl = _TG_TEMPLATES.get(template_key)
            if not tpl:
                return
            if not _is_in_chat(chat_id):
                _set_chat_meeting_id(chat_id, meeting.id)

            # Delete previous template response (reduce clutter)
            old_msg = _last_template_msg.pop(chat_id, None)
            if old_msg:
                try:
                    await _tg_api("deleteMessage", chat_id=chat_id, message_id=old_msg)
                except Exception:
                    pass

            # Send placeholder immediately (prevents "frozen" feeling)
            placeholder = await _tg_send(chat_id, f"⏳ Формирую {tpl['name'].lower()}...")
            placeholder_id = placeholder.get("result", {}).get("message_id") if placeholder else None

            asyncio.create_task(_handle_template_response(
                chat_id, tpl["prompt"], meeting.id, placeholder_id))
        elif action == "exit":
            _set_chat_meeting_id(chat_id, None)
            await _tg_send(chat_id, "Чат завершён. Отправьте аудио для новой транскрипции.",
                           reply_markup=MAIN_KEYBOARD)
    finally:
        db.close()


# ──────────────── Voice question in chat mode ────────────────

async def _transcribe_voice_groq(file_id: str) -> str | None:
    """Download voice from Telegram and transcribe via Groq Whisper API.

    Fast path for short voice questions: ~1-3 sec vs RunPod cold-start 30-60 sec.
    Downloads file to memory (no disk I/O), sends directly to Groq.
    Returns transcribed text or None on failure.
    """
    from app.config import GROQ_API_KEY
    if not GROQ_API_KEY:
        return None

    # Get Telegram file download URL
    result = await _tg_api("getFile", file_id=file_id)
    tg_file_path = result.get("result", {}).get("file_path")
    if not tg_file_path:
        logger.error(f"Groq fast path: getFile failed: {result}")
        return None

    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{tg_file_path}"

    async with httpx.AsyncClient(timeout=30) as client:
        # Download to memory
        resp = await client.get(download_url)
        resp.raise_for_status()
        audio_bytes = resp.content

    # Send to Groq Whisper API
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
            data={"model": "whisper-large-v3", "language": "ru", "response_format": "text"},
        )
        resp.raise_for_status()
        text = resp.text.strip()

    logger.info(f"Groq Whisper transcribed {len(audio_bytes)} bytes → {len(text)} chars")
    return text or None


async def _handle_voice_question(chat_id: str, file_id: str, file_size: int, message_id: int):
    """Transcribe voice message and send as AI chat question."""
    try:

        # Fast path: Groq Whisper API (1-3 sec, in-memory, no disk I/O)
        async with _typing_loop(chat_id):
            text = await _transcribe_voice_groq(file_id)

        if not text:
            # Fallback: RunPod (slower — warn user)
            await _tg_send(chat_id, "⏳ Распознаю голосовое, подождите немного...")
            import tempfile, shutil
            tmp_dir = tempfile.mkdtemp()
            voice_path = f"{tmp_dir}/voice.ogg"
            success = await _tg_download_file(file_id, voice_path, file_size=file_size,
                                              message_id=message_id, chat_id=chat_id)
            if success:
                from app.services.transcriber import transcribe_file
                result = await transcribe_file(voice_path)
                text = result.get("full_text", "").strip()
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if not text:
            await _tg_send(chat_id, "Не удалось распознать голосовое. Попробуйте текстом.")
            return

        # Show what was recognized, then send as AI question
        await _tg_send(chat_id, f"🎤 _{text}_", parse_mode="Markdown")
        await _handle_chat_message(chat_id, text)

    except Exception as e:
        logger.error(f"Voice question error: {e}", exc_info=True)
        await _tg_send(chat_id, "Ошибка распознавания голосового. Попробуйте текстом.")


# ──────────────── Chat mode ────────────────

# Telegram-adapted templates (2 core buttons)
_TG_TEMPLATES = {
    "tasks": {
        "name": "Задачи",
        "prompt": "Извлеки ВСЕ задачи из встречи. Формат: нумерованный список, одна задача = одна строка. Формат строки: «1. Ответственный — Задача (дедлайн)». Если дедлайн не указан — не пиши. ЗАПРЕЩЕНО: markdown-таблицы (символ |). Только факты из транскрипта.",
    },
    "summary": {
        "name": "Резюме",
        "prompt": "Дай краткое структурированное резюме встречи: ключевые темы, решения, задачи. Списки через •. ЗАПРЕЩЕНО: markdown-таблицы (символ |), заголовки (# ##). Только факты из транскрипта.",
    },
}

# Track last template/welcome message for cleanup
_last_template_msg: dict[str, int] = {}
_last_chat_welcome: dict[str, int] = {}


def _chat_keyboard(meeting_id: int) -> dict:
    """Inline keyboard for AI chat mode — 2 templates + exit."""
    return {"inline_keyboard": [
        [
            {"text": "✅ Задачи", "callback_data": f"tpl:{meeting_id}:tasks"},
            {"text": "📄 Резюме", "callback_data": f"tpl:{meeting_id}:summary"},
        ],
        [{"text": "❌ Завершить чат", "callback_data": f"exit:{meeting_id}"}],
    ]}


async def _handle_template_response(chat_id: str, prompt: str, meeting_id: int,
                                     placeholder_id: int | None):
    """Run template, replace placeholder with result, track for cleanup."""
    try:
        # Use chat handler with is_template=True (bypasses limits, doesn't count)
        await _handle_chat_message(chat_id, prompt, is_template=True)

        # Delete placeholder after answer is sent
        if placeholder_id:
            try:
                await _tg_api("deleteMessage", chat_id=chat_id, message_id=placeholder_id)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Template error: {e}", exc_info=True)
        if placeholder_id:
            try:
                await _tg_api("deleteMessage", chat_id=chat_id, message_id=placeholder_id)
            except Exception:
                pass
        await _tg_send(chat_id, "AI-ассистент временно недоступен. Попробуйте через минуту.")


async def _enter_chat_mode(chat_id: str, meeting: Meeting, user: User):
    """Enter AI chat mode for a specific meeting."""
    if not meeting.transcript or not meeting.transcript.full_text:
        await _tg_send(chat_id, "Транскрипт ещё не готов. Подождите завершения обработки.")
        return

    # Set chat state in DB (persists across restarts)
    _set_chat_meeting_id(chat_id, meeting.id)

    # Delete previous welcome message (prevents accumulation)
    old_welcome = _last_chat_welcome.pop(chat_id, None)
    if old_welcome:
        try:
            await _tg_api("deleteMessage", chat_id=chat_id, message_id=old_welcome)
        except Exception:
            pass

    title = meeting.title or "Запись"

    # Switch bottom keyboard to chat mode
    await _tg_send(chat_id, f"💬 *AI-чат:* {title}", reply_markup=CHAT_KEYBOARD)

    msg = "Задайте вопрос текстом или голосовым 🎤\nИли нажмите шаблон:"
    result = await _tg_send(chat_id, msg, reply_markup=_chat_keyboard(meeting.id))
    if result and result.get("ok"):
        _last_chat_welcome[chat_id] = result["result"]["message_id"]


async def _handle_chat_message(chat_id: str, text: str, is_template: bool = False):
    """Handle text message in chat mode — send to AI."""
    meeting_id = _get_chat_meeting_id(chat_id)
    if not meeting_id:
        return

    # Per-chat lock for race condition protection
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    lock = _chat_locks[chat_id]

    async with lock:
        db = SessionLocal()
        try:
            user = _find_user_by_chat_id(chat_id, db)
            if not user:
                return

            meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            if not meeting or not meeting.transcript:
                _set_chat_meeting_id(chat_id, None)
                await _tg_send(chat_id, "Встреча не найдена. Чат завершён.")
                return

            # Check per-meeting AI chat limit (templates bypass)
            ok, used, limit, warning = _check_chat_limit(meeting, user)
            if not ok and not is_template:
                title = meeting.title or "Запись"
                await _tg_send(
                    chat_id,
                    f"⚡ Вы задали {limit} из {limit} вопросов по записи «{title}».\n\n"
                    f"✅ Шаблоны (Протокол, Задачи) по-прежнему доступны\n"
                    f"✅ Другие записи — свой лимит вопросов\n\n"
                    f"Безлимитный AI-чат — тариф Start, 499 ₽/мес",
                )
                return

            from app.models import ChatMessage, ChatRole

            # Save user question
            db.add(ChatMessage(
                user_id=user.id, meeting_id=meeting_id,
                role=ChatRole.user, content=text,
            ))
            db.commit()

            # Get history
            history = (
                db.query(ChatMessage)
                .filter(ChatMessage.meeting_id == meeting_id, ChatMessage.user_id == user.id)
                .order_by(ChatMessage.created_at)
                .all()
            )

            # Call AI with continuous typing indicator
            from app.services.chat_engine import ask_about_meeting
            async with _typing_loop(chat_id):
                answer = await ask_about_meeting(meeting, history, is_telegram=True)

            # Increment per-meeting counter AFTER successful response (skip templates)
            if not is_template:
                _increment_chat_usage(meeting, db)

            # Append warning if close to limit
            if warning and not is_template:
                answer += f"\n\n---\n_{warning}_"

            # Save AI answer
            db.add(ChatMessage(
                user_id=user.id, meeting_id=meeting_id,
                role=ChatRole.assistant, content=answer,
            ))
            db.commit()

            # Send answer with template buttons
            result = await _send_long_message(chat_id, answer,
                                              reply_markup=_chat_keyboard(meeting_id))

            # Track template response for cleanup on next template click
            if is_template and result and result.get("ok"):
                _last_template_msg[chat_id] = result.get("result", {}).get("message_id", 0)

        except Exception as e:
            logger.error(f"Chat error for meeting {meeting_id}: {e}", exc_info=True)
            await _tg_send(chat_id, "AI-ассистент временно недоступен. Попробуйте через минуту.")
        finally:
            db.close()


async def _send_long_message(chat_id: str, text: str, parse_mode: str = "Markdown",
                             reply_markup: dict | None = None):
    """Send long text, splitting into 4000-char chunks if needed."""
    MAX_LEN = 4000

    if len(text) <= MAX_LEN:
        return await _tg_send(chat_id, text, parse_mode=parse_mode,
                              reply_markup=reply_markup)

    # Split by paragraphs
    chunks = []
    current = ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > MAX_LEN:
            if current:
                chunks.append(current.strip())
            current = para if len(para) <= MAX_LEN else para[:MAX_LEN]
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(current.strip())

    total = len(chunks)
    for i, chunk in enumerate(chunks):
        is_last = i == total - 1
        # Add chunk numbering
        if total > 1:
            chunk = f"({i+1}/{total})\n\n{chunk}"
        markup = reply_markup if is_last else None
        result = await _tg_send(chat_id, chunk, parse_mode=parse_mode, reply_markup=markup)
        if not is_last:
            await asyncio.sleep(0.3)
    return result


# ──────────────── File generation ────────────────

def _generate_transcript_txt(meeting: Meeting) -> io.BytesIO:
    """Generate .txt file with timestamps — our advantage over Bukvitsa."""
    lines = []
    lines.append(f"ZoomHub — Транскрипт встречи")
    lines.append(f"{'=' * 50}")
    lines.append(f"Название: {meeting.title}")
    if meeting.date:
        lines.append(f"Дата: {meeting.date.strftime('%d.%m.%Y %H:%M')}")
    if meeting.duration_seconds:
        lines.append(f"Длительность: {meeting.duration_seconds // 60} мин")
    lines.append("")

    # AI summary
    if meeting.summary and meeting.summary.tldr:
        lines.append("--- КОНСПЕКТ ---")
        lines.append(meeting.summary.tldr)
        lines.append("")
        if meeting.summary.tasks:
            lines.append("ЗАДАЧИ:")
            for t in meeting.summary.tasks:
                task = t.get("task", "") if isinstance(t, dict) else str(t)
                assignee = t.get("assignee", "") if isinstance(t, dict) else ""
                line = f"  [ ] {task}"
                if assignee:
                    line += f" — {assignee}"
                lines.append(line)
            lines.append("")

    # Transcript with timestamps
    lines.append("--- ТРАНСКРИПТ ---")
    lines.append("")

    if meeting.transcript and meeting.transcript.segments:
        for seg in meeting.transcript.segments:
            start = seg.get("start", 0)
            h, remainder = divmod(int(start), 3600)
            m, s = divmod(remainder, 60)
            tc = f"[{h:02d}:{m:02d}:{s:02d}]"
            text = seg.get("text", "")
            speaker = seg.get("speaker", "")
            prefix = f"{tc} {speaker}: " if speaker else f"{tc} "
            lines.append(f"{prefix}{text}")
    elif meeting.transcript and meeting.transcript.full_text:
        lines.append(meeting.transcript.full_text)

    lines.append("")
    lines.append("=" * 50)
    lines.append("Транскрибировано в ZoomHub — zoomhub.ru")

    buf = io.BytesIO()
    buf.write("\n".join(lines).encode("utf-8"))
    buf.seek(0)
    return buf


async def _handle_download(chat_id: str, meeting: Meeting, fmt: str | None):
    """Send transcript file to Telegram."""
    if fmt != "txt":
        await _tg_send(chat_id, "Неизвестный формат.")
        return

    if not meeting.transcript:
        await _tg_send(chat_id, "Транскрипт пуст.")
        return

    buf = _generate_transcript_txt(meeting)
    filename = f"{meeting.title[:40]}.txt"
    chars = len(meeting.transcript.full_text) if meeting.transcript.full_text else 0

    await _tg_send_document(
        chat_id, buf, filename,
        caption=f"📄 Транскрипт с таймкодами ({chars:,} символов)\n\nСоздано в *ZoomHub*".replace(",", " ")
    )
