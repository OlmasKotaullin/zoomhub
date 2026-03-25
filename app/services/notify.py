"""Notification service -- Telegram + Email after meeting processing."""
import logging
from app.database import SessionLocal
from app.models import User, Meeting, Summary

logger = logging.getLogger(__name__)

async def notify_user(meeting_id: int):
    """Send notification after meeting is processed."""
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting or not meeting.user_id:
            return

        user = db.query(User).filter(User.id == meeting.user_id).first()
        if not user:
            return

        summary = meeting.summary
        if not summary:
            return

        # Build message
        tasks_text = ""
        if summary.tasks:
            tasks_lines = [f"  • {t.get('task', '')}" + (f" — {t.get('assignee', '')}" if t.get('assignee') else "") for t in summary.tasks[:5]]
            tasks_text = "\n\nЗадачи:\n" + "\n".join(tasks_lines)

        message = f"Встреча обработана: \"{meeting.title}\"\n\n{summary.tldr}{tasks_text}"

        if user.notify_telegram and user.telegram_chat_id:
            await _send_telegram(user.telegram_chat_id, message)

        if user.notify_email:
            await _send_email(user.email, meeting.title, message)
    except Exception as e:
        logger.error(f"Notification error for meeting {meeting_id}: {e}")
    finally:
        db.close()

async def _send_telegram(chat_id: str, message: str):
    """Send message via Telegram Bot API."""
    import httpx
    from app.config import TELEGRAM_BOT_TOKEN

    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient() as client:
            await client.post(url, json={
                "chat_id": chat_id,
                "text": message,
            })
        logger.info(f"Telegram notification sent to {chat_id}")
    except Exception as e:
        logger.error(f"Telegram send error: {e}")

async def _send_email(email: str, subject: str, body: str):
    """Send email notification."""
    try:
        import aiosmtplib
        from email.message import EmailMessage
        from app.config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

        if not SMTP_HOST:
            logger.warning("SMTP not configured")
            return

        msg = EmailMessage()
        msg["From"] = SMTP_USER
        msg["To"] = email
        msg["Subject"] = f"ZoomHub: {subject}"
        msg.set_content(body)

        await aiosmtplib.send(msg, hostname=SMTP_HOST, port=SMTP_PORT,
                              username=SMTP_USER, password=SMTP_PASSWORD, use_tls=True)
        logger.info(f"Email sent to {email}")
    except Exception as e:
        logger.error(f"Email send error: {e}")


async def send_password_reset_email(email: str, reset_url: str) -> bool:
    """Send password reset email. Returns True if sent successfully."""
    try:
        import aiosmtplib
        from email.message import EmailMessage
        from app.config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

        if not SMTP_HOST:
            logger.warning("SMTP not configured — cannot send reset email")
            return False

        msg = EmailMessage()
        msg["From"] = SMTP_USER
        msg["To"] = email
        msg["Subject"] = "ZoomHub: Сброс пароля"
        msg.set_content(
            f"Вы запросили сброс пароля в ZoomHub.\n\n"
            f"Перейдите по ссылке для установки нового пароля:\n{reset_url}\n\n"
            f"Ссылка действительна 1 час.\n\n"
            f"Если вы не запрашивали сброс пароля, проигнорируйте это письмо."
        )

        await aiosmtplib.send(msg, hostname=SMTP_HOST, port=SMTP_PORT,
                              username=SMTP_USER, password=SMTP_PASSWORD, use_tls=True)
        logger.info(f"Password reset email sent to {email}")
        return True
    except Exception as e:
        logger.error(f"Password reset email error: {e}")
        return False
