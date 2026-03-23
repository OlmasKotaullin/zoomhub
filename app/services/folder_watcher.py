"""Мониторинг локальной папки Zoom на новые записи.

Каждые N секунд проверяет /Users/angel/Documents/Zoom/ на новые папки.
Новая запись → берёт audio*.m4a → отправляет в Буквицу → Claude саммари → в ZoomHub.

Формат папки Zoom:
    2024-01-18 12.06.58 Название встречи/
    ├── audio1234567890.m4a
    ├── video1234567890.mp4
    ├── chat.txt (опционально)
    └── recording.conf
"""

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from app.database import SessionLocal
from app.models import Meeting, MeetingSource, MeetingStatus, Folder
from app.services.pipeline import process_meeting
from app.config import RECORDINGS_DIR

logger = logging.getLogger(__name__)

ZOOM_FOLDER = Path("/Users/angel/Documents/Zoom")
POLL_INTERVAL = 30  # секунд — проверяем каждые 30с (локальная папка, быстро)
DEFAULT_FOLDER_NAME = "Zoom-встречи"
DEFAULT_FOLDER_ICON = "📹"

# Хранит пути уже обработанных папок
_processed_dirs: set[str] = set()


async def start_folder_watcher():
    """Запускает мониторинг локальной папки Zoom."""
    if not ZOOM_FOLDER.exists():
        logger.warning(f"Папка Zoom не найдена: {ZOOM_FOLDER}")
        return

    logger.info(f"Folder watcher запущен — мониторю {ZOOM_FOLDER} (интервал {POLL_INTERVAL}с)")

    _load_processed_dirs()

    while True:
        try:
            await _check_new_recordings()
        except Exception as e:
            logger.error(f"Folder watcher ошибка: {e}")

        await asyncio.sleep(POLL_INTERVAL)


def _load_processed_dirs():
    """Загружает пути уже обработанных записей из БД."""
    db = SessionLocal()
    try:
        meetings = (
            db.query(Meeting.audio_path)
            .filter(Meeting.source == MeetingSource.zoom)
            .filter(Meeting.audio_path.isnot(None))
            .all()
        )
        for (path,) in meetings:
            if path:
                # Сохраняем имя родительской папки Zoom
                parent = Path(path).parent
                if parent.name != "recordings":
                    _processed_dirs.add(parent.name)
                # Также сохраняем zoom_source_dir если есть
                _processed_dirs.add(Path(path).stem)
        logger.info(f"Загружено {len(_processed_dirs)} обработанных записей")
    finally:
        db.close()


async def _check_new_recordings():
    """Проверяет папку Zoom на новые записи."""
    if not ZOOM_FOLDER.exists():
        return

    today = datetime.now().strftime("%Y-%m-%d")

    for item in sorted(ZOOM_FOLDER.iterdir()):
        if not item.is_dir():
            continue
        if item.name.startswith("."):
            continue
        # СТРОГО только сегодняшние записи
        if not item.name.startswith(today):
            continue
        if item.name in _processed_dirs:
            continue

        # Ищем аудио-файл
        audio_file = _find_audio(item)
        if not audio_file:
            continue

        # Проверяем что файл не пишется прямо сейчас (размер стабилен)
        if not await _is_file_complete(audio_file):
            continue

        # Парсим название и дату из имени папки
        title, date = _parse_folder_name(item.name)

        logger.info(f"Новая запись: {title} — {audio_file.name}")

        # Копируем файл в data/recordings/ и создаём встречу
        db = SessionLocal()
        try:
            folder = _get_or_create_zoom_folder(db)

            meeting = Meeting(
                title=title,
                folder_id=folder.id,
                source=MeetingSource.zoom,
                zoom_meeting_id=item.name,
                audio_path=str(audio_file),
                status=MeetingStatus.transcribing,
                date=date,
            )
            db.add(meeting)
            db.commit()
            db.refresh(meeting)

            meeting_id = meeting.id
        finally:
            db.close()

        _processed_dirs.add(item.name)

        # Запускаем pipeline ПОСЛЕДОВАТЕЛЬНО (одна за раз, чтобы не было database locked)
        await process_meeting(meeting_id)


def _find_audio(folder: Path) -> Path | None:
    """Находит аудио-файл в папке записи Zoom."""
    # Приоритет: m4a (меньше размер) > mp4
    for ext in ["*.m4a", "*.mp4", "*.mp3"]:
        files = list(folder.glob(ext))
        if files:
            return files[0]
    return None


async def _is_file_complete(path: Path, wait: int = 5) -> bool:
    """Проверяет что файл дозаписан (размер не меняется)."""
    size1 = path.stat().st_size
    if size1 == 0:
        return False
    await asyncio.sleep(wait)
    size2 = path.stat().st_size
    return size1 == size2


def _parse_folder_name(name: str) -> tuple[str, datetime]:
    """Парсит имя папки Zoom → (title, date).

    Формат: '2024-01-18 12.06.58 Название встречи участник'
    """
    # Извлекаем дату
    date_match = re.match(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}\.\d{2}\.\d{2})\s+(.*)', name)
    if date_match:
        date_str = f"{date_match.group(1)} {date_match.group(2).replace('.', ':')}"
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            date = datetime.now()
        title = date_match.group(3).strip()

        # Убираем email и лишнюю информацию из названия
        title = re.sub(r'\S+@\S+\.\S+', '', title).strip()
        title = re.sub(r'\s+', ' ', title).strip()

        if not title or title == "Зал персональной конференции":
            title = f"Zoom-встреча {date_match.group(1)}"
    else:
        title = name
        date = datetime.now()

    return title, date


def _get_or_create_zoom_folder(db) -> Folder:
    """Получает или создаёт папку для Zoom-записей."""
    folder = db.query(Folder).filter(Folder.name == DEFAULT_FOLDER_NAME).first()
    if not folder:
        folder = Folder(name=DEFAULT_FOLDER_NAME, icon=DEFAULT_FOLDER_ICON)
        db.add(folder)
        db.commit()
        db.refresh(folder)
        logger.info(f"Создана папка: {DEFAULT_FOLDER_ICON} {DEFAULT_FOLDER_NAME}")
    return folder
