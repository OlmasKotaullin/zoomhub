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
            await _tg_send(chat_id, "Чат завершён. Отправьте аудио для новой транскрипции.")
        return {"ok": True}

    if text.startswith("/plan"):
        await _handle_plan(chat_id)
        return {"ok": True}

    # Priority 2: Media handling (depends on chat mode)
    file_id, filename, file_size = _extract_media(message)
    message_id = message.get("message_id", 0)
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

    # Priority 4: Text without chat mode → hint
    if text and not text.startswith("/"):
        await _tg_send(
            chat_id,
            "Отправьте аудио или видео для транскрипции.\n"
            "Поддерживаемые форматы: MP3, M4A, MP4, WAV, OGG, WebM."
        )

    return {"ok": True}


# ──────────────── Command handlers ────────────────

async def _handle_start(chat_id: str, text: str):
    """Handle /start [token] — link Telegram to ZoomHub account."""
    from app.auth import decode_token

    parts = text.split(maxsplit=1)
    token = parts[1] if len(parts) > 1 else ""

    db = SessionLocal()
    try:
        if token:
            user_id = decode_token(token)
            if user_id:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    user.telegram_chat_id = chat_id
                    user.notify_telegram = True
                    db.commit()
                    await _tg_send(
                        chat_id,
                        f"*Привет, {user.name}!* 👋\n\n"
                        f"Telegram подключён к ZoomHub.\n\n"
                        f"Что умеет бот:\n"
                        f"• Отправьте аудио/видео — конспект с задачами за 2-3 мин\n"
                        f"• Транскрипт с таймкодами (.txt)\n"
                        f"• AI-чат: задайте вопрос по записи на сайте\n\n"
                        f"Бесплатно {user.plan_hours_limit or 4} ч/мес. Попробуйте — отправьте первый файл!"
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
            # No token — check if already linked
            user = _find_user_by_chat_id(chat_id, db)
            if user:
                await _tg_send(
                    chat_id,
                    f"*Привет, {user.name}!*\n\n"
                    f"Ваш аккаунт уже подключён.\n"
                    f"Отправьте аудио или видео для транскрипции."
                )
            else:
                await _tg_send(
                    chat_id,
                    "*ZoomHub* — рабочая память ваших встреч\n\n"
                    "Отправьте аудио или видео — за 2-3 минуты получите:\n"
                    "• AI-конспект с задачами и темами\n"
                    "• Транскрипт с таймкодами\n"
                    "• AI-чат: задавайте вопросы по записи\n\n"
                    "Бесплатно: 4 часа транскрипции в месяц.\n\n"
                    "Как начать:\n"
                    "1. Зарегистрируйтесь на сайте\n"
                    "2. В настройках нажмите «Подключить Telegram»\n"
                    "3. Перейдите по ссылке из настроек",
                    reply_markup={"inline_keyboard": [
                        [{"text": "Зарегистрироваться", "url": APP_URL}],
                        [{"text": "У меня есть аккаунт", "url": f"{APP_URL}/settings"}],
                    ]}
                )
    finally:
        db.close()


async def _handle_help(chat_id: str):
    """Send help message."""
    await _tg_send(
        chat_id,
        "*ZoomHub — рабочая память встреч*\n\n"
        "Отправьте аудио или видео — получите:\n"
        "• Транскрипт с таймкодами\n"
        "• AI-конспект с задачами и темами\n"
        "• AI-чат: задавайте вопросы по записи\n\n"
        "*Команды:*\n"
        "/plan — тариф и лимиты\n"
        "/exit — выйти из AI-чата\n\n"
        "Форматы: MP3, M4A, MP4, WAV, OGG, WebM (до 2 ГБ)\n\n"
        f"Сайт: {APP_URL}"
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

        await _tg_send(chat_id, msg)
    finally:
        db.close()


# ──────────────── Media processing ────────────────

async def _handle_media(chat_id: str, file_id: str, filename: str, file_size: int, message_id: int = 0):
    """Download media from Telegram, create Meeting, run pipeline."""
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
            # Template button pressed — enter chat mode if not already, then run template
            template_key = extra
            tpl = _TG_TEMPLATES.get(template_key)
            if not tpl:
                return
            if not _is_in_chat(chat_id):
                _set_chat_meeting_id(chat_id, meeting.id)
            asyncio.create_task(_handle_chat_message(chat_id, tpl["prompt"], is_template=True))
        elif action == "exit":
            _set_chat_meeting_id(chat_id, None)
            await _tg_send(chat_id, "Чат завершён. Отправьте аудио для новой транскрипции.")
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

# Telegram-adapted templates (shorter prompts, no markdown headers)
_TG_TEMPLATES = {
    "follow_up": {"name": "Follow Up", "prompt": "Определи все задачи, ответственных и следующие шаги по этой встрече. Используй только факты из транскрипта."},
    "summary": {"name": "Резюме", "prompt": "Дай структурированное резюме встречи: ключевые темы, решения, задачи. Только факты из транскрипта."},
    "protocol": {"name": "Протокол", "prompt": "Составь формальный протокол встречи: дата, участники, повестка, решения, задачи с ответственными и сроками. Только факты из транскрипта."},
    "tasks": {"name": "Трекер задач", "prompt": "Извлеки все задачи из встречи в виде таблицы: номер, ответственный, задача, дедлайн. Только факты из транскрипта."},
}


def _chat_keyboard(meeting_id: int) -> dict:
    """Inline keyboard for AI chat mode — templates + exit."""
    return {"inline_keyboard": [
        [
            {"text": "📋 Протокол", "callback_data": f"tpl:{meeting_id}:protocol"},
            {"text": "✅ Задачи", "callback_data": f"tpl:{meeting_id}:tasks"},
        ],
        [
            {"text": "📊 Follow Up", "callback_data": f"tpl:{meeting_id}:follow_up"},
            {"text": "📄 Резюме", "callback_data": f"tpl:{meeting_id}:summary"},
        ],
        [{"text": "❌ Завершить чат", "callback_data": f"exit:{meeting_id}"}],
    ]}


async def _enter_chat_mode(chat_id: str, meeting: Meeting, user: User):
    """Enter AI chat mode for a specific meeting."""
    if not meeting.transcript or not meeting.transcript.full_text:
        await _tg_send(chat_id, "Транскрипт ещё не готов. Подождите завершения обработки.")
        return

    # Set chat state in DB (persists across restarts)
    old_meeting = _get_chat_meeting_id(chat_id)
    _set_chat_meeting_id(chat_id, meeting.id)

    title = meeting.title or "Запись"
    msg = f"💬 *AI-чат:* {title}\n\n"
    if old_meeting and old_meeting != meeting.id:
        msg += f"_Переключился с другой записи._\n\n"
    msg += "Задайте вопрос текстом или голосовым 🎤\nИли нажмите шаблон:"

    await _tg_send(chat_id, msg, reply_markup=_chat_keyboard(meeting.id))


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
            await _send_long_message(chat_id, answer,
                                     reply_markup=_chat_keyboard(meeting_id))

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
        await _tg_send(chat_id, chunk, parse_mode=parse_mode, reply_markup=markup)
        if not is_last:
            await asyncio.sleep(0.3)


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
