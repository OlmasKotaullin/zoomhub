import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from Application Support (bundled mode) then from cwd (dev mode)
_app_support_env = Path.home() / "Library" / "Application Support" / "ZoomHub" / ".env"
if _app_support_env.exists():
    load_dotenv(_app_support_env)
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Data directory: ZOOMHUB_DATA_DIR overrides for bundled .app mode
_data_dir_override = os.environ.get("ZOOMHUB_DATA_DIR")
if _data_dir_override:
    DATA_DIR = Path(_data_dir_override)
else:
    DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR = DATA_DIR / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)
LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "zoomhub.db"

# Database: PostgreSQL (prod) or SQLite (dev)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
# Fly.io uses postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Auth
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
ACCESS_TOKEN_EXPIRE_HOURS = int(os.environ.get("ACCESS_TOKEN_EXPIRE_HOURS", "720"))  # 30 days

# API Keys
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID") or "0")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
BUKVITSA_BOT_USERNAME = os.environ.get("BUKVITSA_BOT_USERNAME", "")

ZOOM_CLIENT_ID = os.environ.get("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.environ.get("ZOOM_CLIENT_SECRET", "")
ZOOM_ACCOUNT_ID = os.environ.get("ZOOM_ACCOUNT_ID", "")
ZOOM_USER_EMAIL = os.environ.get("ZOOM_USER_EMAIL", "")

# --- Providers ---
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "auto")  # auto | claude | ollama | gemini | groq
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
AUTO_ROUTING_THRESHOLD = 10000  # символов — выше → Claude, ниже → Ollama
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

TRANSCRIPTION_PROVIDER = os.environ.get("TRANSCRIPTION_PROVIDER", "bukvitsa")  # bukvitsa | whisper | openai_whisper
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")

ALLOWED_EXTENSIONS = {".mp4", ".mp3", ".wav", ".m4a", ".webm", ".ogg"}
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

# OAuth
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
YANDEX_CLIENT_ID = os.environ.get("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.environ.get("YANDEX_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")

# Telegram Bot (for notifications)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# SMTP (for email notifications)
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

# Access control
REQUIRE_INVITE_CODE = os.environ.get("REQUIRE_INVITE_CODE", "false").lower() in ("1", "true", "yes")

# Mode
DOCKER_MODE = os.environ.get("DOCKER_MODE", "").lower() in ("1", "true", "yes")
