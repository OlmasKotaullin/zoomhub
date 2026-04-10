"""Zoom API — Server-to-Server OAuth, скачивание записей, polling."""

import logging
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import (
    ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_ACCOUNT_ID,
    ZOOM_USER_EMAIL, RECORDINGS_DIR,
)

logger = logging.getLogger(__name__)

# Кэш токена
_token_cache = {"access_token": "", "expires_at": 0.0}


def is_configured() -> bool:
    """Проверяет что Zoom настроен."""
    return bool(ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET and ZOOM_ACCOUNT_ID)


async def get_access_token() -> str:
    """Получает access_token через Server-to-Server OAuth.

    Zoom S2S OAuth: POST https://zoom.us/oauth/token
    с grant_type=account_credentials и account_id.
    Токен живёт 1 час — кэшируем.
    """
    import time
    now = time.time()

    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    credentials = b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://zoom.us/oauth/token",
            headers={"Authorization": f"Basic {credentials}"},
            data={
                "grant_type": "account_credentials",
                "account_id": ZOOM_ACCOUNT_ID,
            },
        )
        response.raise_for_status()
        data = response.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)

    logger.info("Zoom access_token получен")
    return data["access_token"]


async def get_recent_recordings(from_date: str = "") -> list[dict]:
    """Получает список записей за последние 24 часа.

    Returns:
        Список записей: [{id, topic, start_time, recording_files: [{download_url, file_type, ...}]}]
    """
    token = await get_access_token()

    if not from_date:
        yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
        from_date = yesterday.strftime("%Y-%m-%d")

    # Сначала получаем user_id через /users/me (надёжнее чем email)
    async with httpx.AsyncClient() as client:
        me_resp = await client.get(
            "https://api.zoom.us/v2/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        me_resp.raise_for_status()
        user_id = me_resp.json()["id"]

        response = await client.get(
            f"https://api.zoom.us/v2/users/{user_id}/recordings",
            headers={"Authorization": f"Bearer {token}"},
            params={"from": from_date, "page_size": 30},
        )
        response.raise_for_status()
        data = response.json()

    meetings = data.get("meetings", [])
    logger.info(f"Zoom: найдено {len(meetings)} записей с {from_date}")
    return meetings


async def download_recording(meeting_id: int, download_url: str, access_token: str | None = None) -> Path:
    """Скачивает запись Zoom по URL с авторизацией."""
    token = access_token or await get_access_token()

    meeting_dir = RECORDINGS_DIR / str(meeting_id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    file_path = meeting_dir / "original.mp4"

    async with httpx.AsyncClient(follow_redirects=True, timeout=600) as client:
        response = await client.get(
            download_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()

        with open(file_path, "wb") as f:
            f.write(response.content)

    size_mb = file_path.stat().st_size / (1024 * 1024)
    logger.info(f"Запись скачана: {file_path} ({size_mb:.1f} MB)")
    return file_path
