"""Фоновый polling Zoom API — проверяет новые записи каждые N минут.

Полный автоматический цикл:
1. Zoom звонок завершён → Zoom сохраняет запись (1-5 мин)
2. Poller обнаруживает новую запись
3. Скачивает аудио/видео
4. Отправляет в Буквицу → получает транскрипт
5. Claude делает саммари
6. Всё сохраняется в рабочую папку
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import Meeting, MeetingSource, MeetingStatus, Folder
from app.services.zoom_client import is_configured, get_recent_recordings
from app.services.pipeline import process_meeting

logger = logging.getLogger(__name__)

POLL_INTERVAL = 120  # секунд (2 минуты)
DEFAULT_FOLDER_NAME = "Zoom-встречи"
DEFAULT_FOLDER_ICON = "📹"

# Хранит zoom_meeting_id уже обработанных записей (чтобы не дублировать)
_processed_ids: set[str] = set()


async def start_polling():
    """Запускает бесконечный цикл проверки новых записей Zoom."""
    if not is_configured():
        logger.warning(
            "Zoom polling не запущен — не заполнены ZOOM_CLIENT_ID, "
            "ZOOM_CLIENT_SECRET, ZOOM_ACCOUNT_ID в .env"
        )
        return

    logger.info(f"Zoom polling запущен (интервал {POLL_INTERVAL}с)")

    # Загружаем уже обработанные zoom_meeting_id из БД
    _load_processed_ids()

    while True:
        try:
            await _check_new_recordings()
        except Exception as e:
            logger.error(f"Zoom polling ошибка: {e}")

        await asyncio.sleep(POLL_INTERVAL)


def _load_processed_ids():
    """Загружает ID уже обработанных Zoom-записей из БД."""
    db = SessionLocal()
    try:
        meetings = (
            db.query(Meeting.zoom_meeting_id)
            .filter(Meeting.source == MeetingSource.zoom)
            .filter(Meeting.zoom_meeting_id.isnot(None))
            .all()
        )
        for (zoom_id,) in meetings:
            _processed_ids.add(zoom_id)
        logger.info(f"Загружено {len(_processed_ids)} обработанных Zoom-записей")
    finally:
        db.close()


async def _check_new_recordings():
    """Проверяет Zoom API на новые записи и запускает обработку."""
    recordings = await get_recent_recordings()

    for recording in recordings:
        zoom_uuid = str(recording.get("uuid", ""))
        zoom_id = str(recording.get("id", ""))
        topic = recording.get("topic", "Zoom-встреча")

        # Уникальный ключ — uuid записи
        unique_key = zoom_uuid or zoom_id
        if unique_key in _processed_ids:
            continue

        # Ищем аудио/видео файл для скачивания
        recording_files = recording.get("recording_files", [])
        download_file = _pick_best_file(recording_files)

        if not download_file:
            logger.debug(f"Пропускаем {topic} — нет аудио/видео файлов")
            _processed_ids.add(unique_key)
            continue

        download_url = download_file.get("download_url", "")
        if not download_url:
            continue

        logger.info(f"Новая запись Zoom: {topic} — запускаю обработку")

        # Создаём встречу в БД
        db = SessionLocal()
        try:
            folder = _get_or_create_zoom_folder(db)

            meeting = Meeting(
                title=topic,
                folder_id=folder.id,
                source=MeetingSource.zoom,
                zoom_meeting_id=unique_key,
                status=MeetingStatus.downloading,
                date=_parse_zoom_date(recording.get("start_time", "")),
            )
            db.add(meeting)
            db.commit()
            db.refresh(meeting)

            meeting_id = meeting.id
        finally:
            db.close()

        _processed_ids.add(unique_key)

        # Запускаем полный pipeline в фоне
        asyncio.create_task(process_meeting(meeting_id, download_url=download_url))


def _pick_best_file(recording_files: list[dict]) -> dict | None:
    """Выбирает лучший файл для транскрибации (аудио предпочтительнее видео)."""
    # Приоритет: M4A (аудио, меньше размер) > MP4 (видео)
    for file_type in ["M4A", "MP4"]:
        for f in recording_files:
            if f.get("file_type") == file_type and f.get("status") == "completed":
                return f

    # Fallback — любой завершённый файл
    for f in recording_files:
        if f.get("status") == "completed" and f.get("file_type") in ("M4A", "MP4", "MP3"):
            return f

    return None


def _get_or_create_zoom_folder(db) -> Folder:
    """Получает или создаёт папку для автоматических Zoom-записей."""
    folder = db.query(Folder).filter(Folder.name == DEFAULT_FOLDER_NAME).first()
    if not folder:
        folder = Folder(name=DEFAULT_FOLDER_NAME, icon=DEFAULT_FOLDER_ICON)
        db.add(folder)
        db.commit()
        db.refresh(folder)
        logger.info(f"Создана папка: {DEFAULT_FOLDER_ICON} {DEFAULT_FOLDER_NAME}")
    return folder


def _parse_zoom_date(date_str: str) -> datetime:
    """Парсит дату из Zoom API (ISO 8601)."""
    if not date_str:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)
