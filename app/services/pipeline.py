"""Background pipeline: обработка записи встречи."""

import asyncio
import logging
import subprocess
from pathlib import Path

from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models import Meeting, MeetingStatus, Transcript, Summary, User
from app.services.transcriber import transcribe_file
from app.services.summarizer import generate_summary
from app.services.notify import notify_user

logger = logging.getLogger(__name__)

MAX_DB_RETRIES = 5
DB_RETRY_DELAY = 2


def _db_retry(func):
    """Retry-обёртка для DB-операций при 'database is locked'."""
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_DB_RETRIES):
            try:
                return func(*args, **kwargs)
            except OperationalError as e:
                if "locked" in str(e) and attempt < MAX_DB_RETRIES - 1:
                    logger.warning(f"DB locked, retry {attempt + 1}/{MAX_DB_RETRIES}...")
                    import time
                    time.sleep(DB_RETRY_DELAY * (attempt + 1))
                else:
                    raise
    return wrapper


@_db_retry
def _save_transcript(meeting_id: int, full_text: str, segments: list):
    db = SessionLocal()
    try:
        existing = db.query(Transcript).filter(Transcript.meeting_id == meeting_id).first()
        if existing:
            existing.full_text = full_text
            existing.segments = segments
        else:
            db.add(Transcript(meeting_id=meeting_id, full_text=full_text, segments=segments))
        db.commit()
    finally:
        db.close()


@_db_retry
def _update_duration_and_usage(meeting_id: int, duration_seconds: int):
    """Save duration to meeting and update user's monthly usage."""
    if not duration_seconds:
        return
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            return
        meeting.duration_seconds = duration_seconds

        # Update user usage
        user = db.query(User).filter(User.id == meeting.user_id).first()
        if user:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            # Reset monthly usage if new month
            if not user.usage_month_start or user.usage_month_start.month != now.month:
                user.usage_seconds_month = 0
                user.usage_month_start = now
            user.usage_seconds_month = (user.usage_seconds_month or 0) + duration_seconds

        db.commit()
    finally:
        db.close()


@_db_retry
def _save_summary(meeting_id: int, data: dict):
    db = SessionLocal()
    try:
        existing = db.query(Summary).filter(Summary.meeting_id == meeting_id).first()
        if existing:
            existing.tldr = data["tldr"]
            existing.tasks = data["tasks"]
            existing.topics = data["topics"]
            existing.insights = data["insights"]
            existing.raw_response = data["raw_response"]
        else:
            db.add(Summary(
                meeting_id=meeting_id,
                tldr=data["tldr"],
                tasks=data["tasks"],
                topics=data["topics"],
                insights=data["insights"],
                raw_response=data["raw_response"],
            ))
        db.commit()
    finally:
        db.close()


@_db_retry
def _update_status(meeting_id: int, status: MeetingStatus, error: str | None = None):
    """Короткая DB-сессия для обновления статуса — не блокирует БД надолго."""
    db = SessionLocal()
    try:
        m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if m:
            m.status = status
            if error is not None:
                m.error_message = error
            db.commit()
    finally:
        db.close()


def _get_audio_path(meeting_id: int) -> str | None:
    db = SessionLocal()
    try:
        m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        return m.audio_path if m else None
    finally:
        db.close()


def _get_user_id(meeting_id: int) -> int | None:
    db = SessionLocal()
    try:
        m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        return m.user_id if m else None
    finally:
        db.close()


@_db_retry
def _update_audio_path(meeting_id: int, audio_path: str):
    db = SessionLocal()
    try:
        m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if m:
            m.audio_path = audio_path
            db.commit()
    finally:
        db.close()


def _get_meeting_source(meeting_id: int) -> str | None:
    db = SessionLocal()
    try:
        m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        return m.source.value if m and m.source else None
    finally:
        db.close()


def _get_duration_ffprobe(audio_path: str) -> int:
    """Get audio duration in seconds via ffprobe. Returns 0 on failure."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=15
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return int(float(probe.stdout.strip()))
    except Exception as e:
        logger.warning(f"ffprobe failed for {audio_path}: {e}")
    return 0


def _is_video_file(file_path: str) -> bool:
    """Check if file is a video format that needs audio extraction."""
    video_exts = {".webm", ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"}
    return Path(file_path).suffix.lower() in video_exts


async def _extract_audio(file_path: str) -> str:
    """Extract audio from video file via ffmpeg. Returns path to audio file.

    350 MB WebM → ~20 MB opus (94% reduction).
    If extraction fails, returns original file.
    """
    src = Path(file_path)
    if not _is_video_file(file_path):
        return file_path

    extracted = src.parent / f"{src.stem}_audio.opus"
    if extracted.exists():
        return str(extracted)

    src_size_mb = src.stat().st_size / 1024 / 1024
    logger.info(f"Extracting audio from video {src.name} ({src_size_mb:.0f} MB)...")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", file_path,
        "-vn",              # no video
        "-ac", "1",         # mono
        "-ar", "16000",     # 16 kHz
        "-c:a", "libopus",  # opus codec
        "-b:a", "24k",      # 24 kbps
        "-y", str(extracted),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    if proc.returncode != 0 or not extracted.exists():
        logger.warning(f"Audio extraction failed (rc={proc.returncode}), using original")
        return file_path

    new_size_mb = extracted.stat().st_size / 1024 / 1024
    reduction = int((1 - new_size_mb / src_size_mb) * 100) if src_size_mb > 0 else 0
    logger.info(f"Audio extracted: {src_size_mb:.0f} MB → {new_size_mb:.1f} MB ({reduction}% reduction)")

    # Delete original video to free disk space (keep only extracted audio)
    try:
        src.unlink()
        logger.info(f"Deleted original video: {src.name} ({src_size_mb:.0f} MB freed)")
    except Exception as e:
        logger.warning(f"Could not delete original video: {e}")

    return str(extracted)


async def process_meeting(meeting_id: int, download_url: str | None = None, access_token: str | None = None):
    """Фоновая обработка записи: скачивание → транскрибация → конспект."""
    try:
        # Шаг 1: Скачивание (если Zoom)
        if download_url:
            try:
                _update_status(meeting_id, MeetingStatus.downloading)
                from app.services.zoom_client import download_recording
                file_path = await download_recording(meeting_id, download_url, access_token=access_token)
                db = SessionLocal()
                try:
                    m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
                    m.audio_path = str(file_path)
                    db.commit()
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Ошибка скачивания: {e}")
                _update_status(meeting_id, MeetingStatus.error, f"Ошибка скачивания: {e}")
                return

        # Шаг 1.5: Извлечение аудио из видео (WebM 350MB → opus 20MB)
        audio_path = _get_audio_path(meeting_id)
        if not audio_path:
            _update_status(meeting_id, MeetingStatus.error, "Нет аудиофайла")
            return

        if _is_video_file(audio_path):
            logger.info(f"[{meeting_id}] Видеофайл — извлекаю аудио...")
            audio_path = await _extract_audio(audio_path)
            # Update path in DB to use extracted audio
            _update_audio_path(meeting_id, audio_path)

        # Шаг 2: Транскрипция
        try:
            _update_status(meeting_id, MeetingStatus.transcribing)
            logger.info(f"[{meeting_id}] Начинаю транскрибацию: {audio_path}")

            user_id = _get_user_id(meeting_id)
            result = await transcribe_file(audio_path, user_id=user_id)

            logger.info(f"[{meeting_id}] Транскрипт получен: {len(result['full_text'])} символов")

            # Сохраняем транскрипт — с retry
            transcript_text = result["full_text"]
            _save_transcript(meeting_id, result["full_text"], result["segments"])

            # Вычисляем duration через ffprobe (провайдеры не возвращают его)
            duration = result.get("duration_seconds", 0) or _get_duration_ffprobe(audio_path)
            if duration:
                logger.info(f"[{meeting_id}] Длительность: {duration} сек ({duration // 60} мин)")
            else:
                logger.warning(f"[{meeting_id}] Не удалось определить длительность аудио")
            _update_duration_and_usage(meeting_id, duration)

        except Exception as e:
            logger.error(f"[{meeting_id}] Ошибка транскрибации: {e}")
            _update_status(meeting_id, MeetingStatus.error, f"Ошибка транскрибации: {e}")
            return

        # Шаг 2.5: Автоклассификация по ключевым словам
        try:
            from app.services.classifier import classify_meeting
            folder_name = classify_meeting(meeting_id)
            if folder_name:
                logger.info(f"[{meeting_id}] Автоклассификация → {folder_name}")
        except Exception as e:
            logger.warning(f"[{meeting_id}] Ошибка классификации: {e}")

        # Шаг 3: Генерация конспекта через Claude
        try:
            _update_status(meeting_id, MeetingStatus.summarizing)
            logger.info(f"[{meeting_id}] Генерирую конспект...")

            summary_data = await generate_summary(transcript_text)

            _save_summary(meeting_id, summary_data)

        except Exception as e:
            logger.warning(f"[{meeting_id}] Ошибка конспекта: {e}. Транскрипт доступен.")

        # Готово
        _update_status(meeting_id, MeetingStatus.ready)
        # Skip notify for telegram-source meetings — telegram_bot.py sends its own result
        source = _get_meeting_source(meeting_id)
        if source != "telegram":
            asyncio.create_task(notify_user(meeting_id))
        logger.info(f"[{meeting_id}] Обработка завершена успешно")

    except Exception as e:
        logger.error(f"[{meeting_id}] Pipeline error: {e}")
        try:
            _update_status(meeting_id, MeetingStatus.error, str(e))
        except Exception:
            pass
        # Alert admins
        asyncio.create_task(_notify_admins_pipeline_error(meeting_id, str(e)))


async def _notify_admins_pipeline_error(meeting_id: int, error: str):
    """Send Telegram alert to all admins when pipeline fails."""
    try:
        import httpx
        from app.config import TELEGRAM_BOT_TOKEN, APP_URL
        if not TELEGRAM_BOT_TOKEN:
            return

        db = SessionLocal()
        try:
            meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            user = db.query(User).filter(User.id == meeting.user_id).first() if meeting else None
            admins = db.query(User).filter(User.is_admin == True).all()

            title = meeting.title if meeting else f"#{meeting_id}"
            user_info = f"{user.name} ({user.email})" if user else "unknown"

            text = (
                f"⚠️ *Ошибка обработки*\n\n"
                f"Встреча: {title}\n"
                f"Юзер: {user_info}\n"
                f"Ошибка: {error[:300]}"
            )

            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            async with httpx.AsyncClient(timeout=10) as client:
                for admin in admins:
                    if admin.telegram_chat_id:
                        await client.post(url, json={
                            "chat_id": admin.telegram_chat_id,
                            "text": text,
                            "parse_mode": "Markdown",
                        })
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to notify admins: {e}")


async def process_meeting_transcript_only(meeting_id: int):
    """Обработка встречи с уже готовым транскриптом (агент сделал транскрипцию локально).
    Запускает только классификацию + суммаризацию."""
    try:
        db = SessionLocal()
        try:
            meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            if not meeting or not meeting.transcript:
                logger.error(f"[{meeting_id}] Нет транскрипта для summary-only")
                return
            transcript_text = meeting.transcript.full_text
        finally:
            db.close()

        # Автоклассификация
        try:
            from app.services.classifier import classify_meeting
            folder_name = classify_meeting(meeting_id)
            if folder_name:
                logger.info(f"[{meeting_id}] Автоклассификация → {folder_name}")
        except Exception as e:
            logger.warning(f"[{meeting_id}] Ошибка классификации: {e}")

        # Генерация саммари
        try:
            _update_status(meeting_id, MeetingStatus.summarizing)
            logger.info(f"[{meeting_id}] Генерирую конспект (transcript-only)...")
            summary_data = await generate_summary(transcript_text)
            _save_summary(meeting_id, summary_data)
        except Exception as e:
            logger.warning(f"[{meeting_id}] Ошибка конспекта: {e}")

        _update_status(meeting_id, MeetingStatus.ready)
        asyncio.create_task(notify_user(meeting_id))
        logger.info(f"[{meeting_id}] Обработка (transcript-only) завершена")

    except Exception as e:
        logger.error(f"[{meeting_id}] Transcript-only pipeline error: {e}")
        try:
            _update_status(meeting_id, MeetingStatus.error, str(e))
        except Exception:
            pass
