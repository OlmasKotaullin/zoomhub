"""Мониторинг простоя локального агента ZoomHub.

Раз в час проверяет, когда последний раз пользователь с agent_api_token
загружал запись через агент. Если больше AGENT_IDLE_ALERT_HOURS — шлёт
в Telegram юзера напоминание.

Причин простоя много: токен истёк, launchd упал, сеть, папка Zoom сменилась,
юзер закрыл Mac. Мы не диагностируем — только говорим «давно не видно», дальше
юзер сам проверит.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import TELEGRAM_BOT_TOKEN
from app.database import SessionLocal
from app.models import Meeting, MeetingSource, User

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 3600          # проверяем раз в час
AGENT_IDLE_ALERT_HOURS = 48       # тишина дольше — тревога
REMINDER_COOLDOWN_HOURS = 24      # не чаще одного напоминания в сутки

# В памяти (сбрасывается при рестарте — это ОК):
_last_alert_at: dict[int, datetime] = {}


async def _notify(chat_id: str, text: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.warning(f"agent_monitor notify failed: {e}")


async def _check_once():
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=AGENT_IDLE_ALERT_HOURS)
    cooldown_threshold = now - timedelta(hours=REMINDER_COOLDOWN_HOURS)

    db = SessionLocal()
    try:
        users = db.query(User).filter(
            User.agent_api_token.isnot(None),
            User.is_active == True,  # noqa: E712
            User.telegram_chat_id.isnot(None),
        ).all()

        for user in users:
            last = _last_alert_at.get(user.id)
            if last and last > cooldown_threshold:
                continue  # уже напоминали недавно

            last_upload = (
                db.query(Meeting)
                .filter(
                    Meeting.user_id == user.id,
                    Meeting.source == MeetingSource.zoom,
                )
                .order_by(Meeting.created_at.desc())
                .first()
            )

            if last_upload and last_upload.created_at > threshold.replace(tzinfo=None):
                continue  # недавно была активность

            hours = int((now - (last_upload.created_at.replace(tzinfo=timezone.utc) if last_upload else now - timedelta(days=999)))
                        .total_seconds() / 3600) if last_upload else 999

            text = (
                f"Локальный ZoomHub-агент молчит уже {hours} ч.\n\n"
                f"Проверь: идёт ли он в процессах (launchctl list | grep zoomhub), "
                f"есть ли новые файлы в папке Zoom, и не сменился ли токен."
            )
            await _notify(user.telegram_chat_id, text)
            _last_alert_at[user.id] = now
            logger.info(f"agent_monitor: alerted user {user.id} (silent {hours}h)")
    finally:
        db.close()


async def start_agent_monitor():
    """Фоновая таска — запускается из lifespan."""
    # Подождать, пока БД инициализируется и сервер поднимется
    await asyncio.sleep(120)
    while True:
        try:
            await _check_once()
        except Exception as e:
            logger.warning(f"agent_monitor loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SEC)
