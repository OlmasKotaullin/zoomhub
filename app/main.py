import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import DOCKER_MODE, SECRET_KEY
from app.database import init_db

# Увеличиваем лимит размера файла для multipart-загрузок (по умолчанию 1MB с python-multipart 0.0.18+)
try:
    from starlette.formparsers import MultiPartParser
    MultiPartParser.max_file_size = 2 * 1024 * 1024 * 1024  # 2 GB
    MultiPartParser.max_part_size = 2 * 1024 * 1024 * 1024  # 2 GB
except (ImportError, AttributeError):
    pass

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

    # Мониторинг локальной папки Zoom (только локальный режим, не Docker)
    if not DOCKER_MODE:
        from app.services.folder_watcher import start_folder_watcher
        tasks.append(asyncio.create_task(start_folder_watcher()))

    # Zoom API polling (legacy S2S, дополнительно, если настроен)
    from app.services.zoom_poller import start_polling
    tasks.append(asyncio.create_task(start_polling()))

    # Per-user Zoom recording poller
    from app.services.zoom_user_poller import start_user_polling
    tasks.append(asyncio.create_task(start_user_polling()))

    # Register Telegram bot commands menu
    try:
        from app.routers.telegram_bot import setup_bot_commands
        await setup_bot_commands()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Bot commands setup: {e}")

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
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not request.url.path.startswith("/static"):
        response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


# Логируем все 422 ошибки для отладки загрузки файлов
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger = logging.getLogger("app.upload")
    logger.error(f"422 Validation Error on {request.method} {request.url.path}: {exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# Auth-free paths
_PUBLIC_PREFIXES = ("/login", "/register", "/logout", "/health", "/static", "/api/auth/", "/auth/", "/zoom/connect", "/api/telegram/webhook", "/onboarding", "/api/temp-audio/")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    from app.deps import get_current_user_optional
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        user = get_current_user_optional(request, db)
    finally:
        db.close()

    if not user:
        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "Не авторизован"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    return await call_next(request)


@app.get("/health")
async def health_check():
    return {"status": "ok", "app": "ZoomHub", "version": "2.0.0"}


static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

from app.routers import auth, folders, meetings, chat, zoom, native_api, admin, telegram_bot  # noqa: E402

app.include_router(telegram_bot.router)  # before auth — handles /api/telegram/webhook
app.include_router(auth.router)
app.include_router(folders.router)
app.include_router(meetings.router)
app.include_router(chat.router)
app.include_router(zoom.router)
app.include_router(zoom._api)
app.include_router(native_api.router)
app.include_router(admin.router)
