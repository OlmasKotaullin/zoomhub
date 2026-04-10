"""Telegram Bot — второй фронтенд ZoomHub.

Пользователь кидает аудио/видео/голосовое в бота →
бот транскрибирует → делает AI-саммари →
результат в Telegram + автоматически на zoomhub.ru (та же БД).

Расширяет существующий webhook из auth.py.
"""

import asyncio
import json
import logging
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
    """Download large file via Telethon MTProto (up to 2 GB).

    Uses the bot's Telethon userbot session to download from the chat
    where the message was sent. Requires message_id and chat_id.
    """
    try:
        from app.services.providers.bukvitsa_provider import _get_client

        client = await _get_client()
        size_mb = file_size / (1024 * 1024)
        logger.info(f"Downloading {size_mb:.1f} MB via Telethon (msg_id={message_id}, chat={chat_id})...")

        if not message_id or not chat_id:
            logger.error("message_id and chat_id required for Telethon download")
            return False

        # Get the message from the chat via Telethon
        peer = int(chat_id)
        messages = await client.get_messages(peer, ids=[message_id])
        if not messages or not messages[0]:
            logger.error(f"Message {message_id} not found in chat {chat_id}")
            return False

        msg = messages[0]
        if not msg.media:
            logger.error(f"Message {message_id} has no media")
            return False

        # Download media via Telethon (supports up to 2 GB)
        downloaded = await client.download_media(msg, file=dest_path)
        if downloaded and Path(downloaded).exists():
            actual_size = Path(downloaded).stat().st_size / (1024 * 1024)
            logger.info(f"Downloaded {actual_size:.1f} MB via Telethon -> {downloaded}")
            # Rename if Telethon saved with different name
            if str(downloaded) != dest_path:
                Path(downloaded).rename(dest_path)
            return True

        logger.error("Telethon download returned None")
        return False

    except Exception as e:
        logger.error(f"Telethon download error: {e}", exc_info=True)
        return False


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


# ──────────────── Webhook handler ────────────────

@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    """Handle all Telegram bot updates."""
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": True}

    body = await request.json()
    message = body.get("message", {})

    if not message:
        return {"ok": True}

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "")

    if not chat_id:
        return {"ok": True}

    # Handle /start command (account linking)
    if text.startswith("/start"):
        await _handle_start(chat_id, text)
        return {"ok": True}

    # Handle /help command
    if text.startswith("/help"):
        await _handle_help(chat_id)
        return {"ok": True}

    # Handle media (audio, video, voice, document)
    file_id, filename, file_size = _extract_media(message)
    message_id = message.get("message_id", 0)
    if file_id:
        asyncio.create_task(_handle_media(chat_id, file_id, filename, file_size, message_id=message_id))
        return {"ok": True}

    # Text message — not supported yet
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
                        f"*Привет, {user.name}!*\n\n"
                        f"Telegram подключён к ZoomHub.\n\n"
                        f"Теперь вы можете:\n"
                        f"• Отправить аудио/видео — получите транскрипт + саммари\n"
                        f"• Результаты автоматически появятся на сайте\n\n"
                        f"Отправьте первый файл!"
                    )
                    return

            await _tg_send(chat_id, "Ссылка устарела. Получите новую в настройках ZoomHub.")
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
                    "Для подключения перейдите по ссылке из настроек ZoomHub.\n\n"
                    f"Ещё нет аккаунта? Зарегистрируйтесь: {APP_URL}"
                )
    finally:
        db.close()


async def _handle_help(chat_id: str):
    """Send help message."""
    await _tg_send(
        chat_id,
        "*ZoomHub — Telegram Bot*\n\n"
        "Отправьте аудио или видео — получите:\n"
        "• Точный транскрипт с таймкодами\n"
        "• AI-саммари с ключевыми идеями и задачами\n\n"
        "Результаты автоматически сохраняются на сайте — "
        "там можно задать вопросы по записи через AI-чат.\n\n"
        "Форматы: MP3, M4A, MP4, WAV, OGG, WebM\n"
        "Лимит: до 2 ГБ\n\n"
        f"Сайт: {APP_URL}"
    )


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

        # Acknowledge receipt
        size_mb = file_size / (1024 * 1024) if file_size else 0
        msg = "Принял! Обрабатываю..."
        if size_mb > 20:
            msg = f"Принял ({size_mb:.0f} МБ)! Скачиваю и обрабатываю — это может занять несколько минут..."
        await _tg_send(chat_id, msg)

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
            await _tg_send(chat_id, "Не удалось скачать файл. Попробуйте ещё раз или загрузите через сайт.")
            return

        meeting.audio_path = str(file_path)
        db.commit()

        logger.info(f"Telegram upload: meeting {meeting.id} from user {user.id} ({filename})")

        # Run pipeline in background (Bukvitsa takes 2-15 min)
        asyncio.create_task(_run_pipeline_and_notify(chat_id, meeting.id))

    except Exception as e:
        logger.error(f"Telegram media handler error: {e}", exc_info=True)
        await _tg_send(chat_id, f"Ошибка обработки: {str(e)[:200]}")
    finally:
        db.close()


async def _run_pipeline_and_notify(chat_id: str, meeting_id: int):
    """Run pipeline in background, then send result to Telegram."""
    try:
        from app.services.pipeline import process_meeting
        await process_meeting(meeting_id)
        await _send_result(chat_id, meeting_id)
    except Exception as e:
        logger.error(f"Pipeline error for meeting {meeting_id}: {e}", exc_info=True)
        await _tg_send(chat_id, f"Ошибка обработки: {str(e)[:200]}")


async def _send_result(chat_id: str, meeting_id: int):
    """Send formatted transcription result back to Telegram."""
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            return

        if meeting.status == MeetingStatus.error:
            await _tg_send(chat_id, f"Ошибка: {meeting.error_message or 'Неизвестная ошибка'}")
            return

        # Build result message
        parts = [f"*{meeting.title}*\n"]

        # Summary
        if meeting.summary:
            s = meeting.summary
            if s.tldr:
                parts.append(f"{s.tldr}\n")

            # Tasks
            if s.tasks:
                parts.append("*Задачи:*")
                for t in s.tasks[:7]:
                    task_text = t.get("task", "") if isinstance(t, dict) else str(t)
                    assignee = t.get("assignee", "") if isinstance(t, dict) else ""
                    line = f"  • {task_text}"
                    if assignee:
                        line += f" — {assignee}"
                    parts.append(line)
                parts.append("")

            # Topics
            if s.topics:
                topics_text = ", ".join(
                    t.get("topic", "") if isinstance(t, dict) else str(t)
                    for t in s.topics[:5]
                )
                if topics_text:
                    parts.append(f"*Темы:* {topics_text}\n")

        # Transcript snippet
        if meeting.transcript and meeting.transcript.full_text:
            text = meeting.transcript.full_text
            if len(text) > 500:
                text = text[:500] + "..."
            parts.append(f"_Транскрипт ({len(meeting.transcript.full_text)} символов):_")
            parts.append(f"```\n{text}\n```")

        message = "\n".join(parts)

        # Truncate if too long for Telegram (4096 chars)
        if len(message) > 4000:
            message = message[:3950] + "\n\n_...полный текст на сайте_"

        # Send with inline button to website
        await _tg_send(
            chat_id,
            message,
            reply_markup={"inline_keyboard": [[{
                "text": "Открыть на сайте",
                "url": f"{APP_URL}/meetings/{meeting_id}"
            }]]}
        )

    finally:
        db.close()
