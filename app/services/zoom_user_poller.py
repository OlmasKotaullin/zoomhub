"""Per-user Zoom recording poller."""
import asyncio
import logging
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import User, Meeting, MeetingSource, MeetingStatus
from app.services.zoom_oauth import get_user_access_token, get_user_recordings
from app.services.pipeline import process_meeting
from app.config import RECORDINGS_DIR

logger = logging.getLogger(__name__)

POLL_INTERVAL = 120  # seconds

async def start_user_polling():
    """Main polling loop -- checks all users with Zoom connected."""
    logger.info("Zoom user poller started")
    await asyncio.sleep(10)  # Wait for app to fully start

    while True:
        try:
            await _poll_all_users()
        except Exception as e:
            logger.error(f"Polling error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

async def _poll_all_users():
    db = SessionLocal()
    try:
        users = db.query(User).filter(
            User.zoom_access_token.isnot(None),
            User.is_active == True,
            User.capture_source.in_(["cloud", "both"]),
        ).all()

        for user in users:
            try:
                await _poll_user(user, db)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"Error polling user {user.id}: {e}")
    finally:
        db.close()

async def _poll_user(user, db):
    access_token = await get_user_access_token(user)
    if not access_token:
        return

    recordings = await get_user_recordings(access_token)

    for recording in recordings:
        zoom_uuid = recording.get("uuid", "")
        zoom_id = str(recording.get("id", ""))
        recording_id = zoom_uuid or zoom_id

        if not recording_id:
            continue

        # Check if already processed
        existing = db.query(Meeting).filter(
            Meeting.zoom_recording_id == recording_id
        ).first()
        if existing:
            continue

        # Find best file (audio preferred)
        files = recording.get("recording_files", [])
        download_file = None
        for f in files:
            if f.get("file_type") in ("M4A", "MP4") and f.get("status") == "completed":
                if not download_file or f.get("file_type") == "M4A":
                    download_file = f

        if not download_file:
            continue

        download_url = download_file.get("download_url", "")
        if not download_url:
            continue

        # Add access token to download URL
        download_url = f"{download_url}?access_token={access_token}"

        title = recording.get("topic", "Zoom Meeting")
        duration = recording.get("duration", 0) * 60  # min to sec

        # Create meeting
        meeting = Meeting(
            user_id=user.id,
            title=title,
            duration_seconds=duration,
            source=MeetingSource.zoom,
            zoom_meeting_id=zoom_id,
            zoom_recording_id=recording_id,
            status=MeetingStatus.downloading,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)

        logger.info(f"New Zoom recording for user {user.id}: {title}")
        asyncio.create_task(process_meeting(meeting.id, download_url))
