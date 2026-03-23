import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    tasks = []

    # Восстанавливаем зависшие встречи (остались в transcribing/downloading после перезапуска)
    tasks.append(asyncio.create_task(_resume_stuck_meetings()))

    # Мониторинг локальной папки Zoom (основной способ)
    from app.services.folder_watcher import start_folder_watcher
    tasks.append(asyncio.create_task(start_folder_watcher()))

    # Zoom API polling (дополнительно, если настроен)
    from app.services.zoom_poller import start_polling
    tasks.append(asyncio.create_task(start_polling()))

    yield

    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


logger = logging.getLogger(__name__)


async def _resume_stuck_meetings():
    """При старте: находит зависшие встречи и перезапускает обработку."""
    await asyncio.sleep(3)  # Даём время Telethon подключиться

    from app.database import SessionLocal
    from app.models import Meeting, MeetingStatus
    from app.services.pipeline import process_meeting
    from app.routers.meetings import _generate_summary_for_meeting

    db = SessionLocal()
    try:
        # Зависшие в transcribing/downloading — полный pipeline
        stuck_pipeline = (
            db.query(Meeting)
            .filter(Meeting.status.in_([MeetingStatus.transcribing, MeetingStatus.downloading]))
            .filter(Meeting.audio_path.isnot(None))
            .all()
        )
        # Зависшие в summarizing — только пересборка саммари
        stuck_summary = (
            db.query(Meeting)
            .filter(Meeting.status == MeetingStatus.summarizing)
            .all()
        )

        total = len(stuck_pipeline) + len(stuck_summary)
        if total:
            logger.info(f"Найдено {total} зависших встреч — перезапускаю обработку")
            for m in stuck_pipeline:
                logger.info(f"  → Pipeline: [{m.id}] {m.title}")
                asyncio.create_task(process_meeting(m.id))
            for m in stuck_summary:
                logger.info(f"  → Саммари: [{m.id}] {m.title}")
                asyncio.create_task(_generate_summary_for_meeting(m.id))
        else:
            logger.info("Зависших встреч нет")
    finally:
        db.close()


app = FastAPI(title="ZoomHub", lifespan=lifespan)


@app.get("/health")
async def health_check():
    """Health check для Tauri — проверяет что бэкенд работает."""
    return {"status": "ok", "app": "ZoomHub"}


static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

from app.routers import folders, meetings, chat, zoom, native_api  # noqa: E402

app.include_router(folders.router)
app.include_router(meetings.router)
app.include_router(chat.router)
app.include_router(zoom.router)
app.include_router(zoom._api)
app.include_router(native_api.router)
