"""Автоклассификация встреч по проектам на основе ключевых слов."""

import logging

from app.database import SessionLocal
from app.models import Meeting, Folder

logger = logging.getLogger(__name__)


def classify_meeting(meeting_id: int) -> str | None:
    """Определяет папку для встречи по ключевым словам в названии и транскрипте.

    Returns:
        Название папки если нашлась, None если нет.
    """
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            return None

        # Уже назначена папка (не Zoom-встречи по умолчанию)
        if meeting.folder and meeting.folder.name != "Zoom-встречи":
            return meeting.folder.name

        # Собираем текст для поиска
        search_text = (meeting.title or "").lower()
        if meeting.transcript and meeting.transcript.full_text:
            search_text += " " + meeting.transcript.full_text[:10000].lower()

        if not search_text.strip():
            return None

        # Ищем совпадения по ключевым словам папок
        folders = db.query(Folder).filter(Folder.keywords.isnot(None), Folder.keywords != "").all()

        best_folder = None
        best_score = 0

        for folder in folders:
            keywords = [kw.strip().lower() for kw in folder.keywords.split(",") if kw.strip()]
            if not keywords:
                continue

            score = sum(1 for kw in keywords if kw in search_text)
            if score > best_score:
                best_score = score
                best_folder = folder

        if best_folder and best_score > 0:
            meeting.folder_id = best_folder.id
            db.commit()
            logger.info(f"[{meeting_id}] Классифицирован → {best_folder.icon} {best_folder.name} (score={best_score})")
            return best_folder.name

        return None
    finally:
        db.close()
