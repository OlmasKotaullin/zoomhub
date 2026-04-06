"""Background pipeline: обработка записи встречи."""

import asyncio
import logging

from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models import Meeting, MeetingStatus, Transcript, Summary
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


async def process_meeting(meeting_id: int, download_url: str | None = None):
    """Фоновая обработка записи: скачивание → транскрибация → конспект."""
    try:
        # Шаг 1: Скачивание (если Zoom)
        if download_url:
            try:
                _update_status(meeting_id, MeetingStatus.downloading)
                from app.services.zoom_client import download_recording
                file_path = await download_recording(meeting_id, download_url)
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

        # Шаг 2: Транскрибация через Буквицу
        audio_path = _get_audio_path(meeting_id)
        if not audio_path:
            _update_status(meeting_id, MeetingStatus.error, "Нет аудиофайла")
            return

        try:
            _update_status(meeting_id, MeetingStatus.transcribing)
            logger.info(f"[{meeting_id}] Начинаю транскрибацию: {audio_path}")

            user_id = _get_user_id(meeting_id)
            result = await transcribe_file(audio_path, user_id=user_id)

            logger.info(f"[{meeting_id}] Транскрипт получен: {len(result['full_text'])} символов")

            # Сохраняем транскрипт — с retry
            transcript_text = result["full_text"]
            _save_transcript(meeting_id, result["full_text"], result["segments"])

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
        asyncio.create_task(notify_user(meeting_id))
        logger.info(f"[{meeting_id}] Обработка завершена успешно")

    except Exception as e:
        logger.error(f"[{meeting_id}] Pipeline error: {e}")
        try:
            _update_status(meeting_id, MeetingStatus.error, str(e))
        except Exception:
            pass


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
