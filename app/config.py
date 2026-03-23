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
DB_URL = f"sqlite:///{DB_PATH}"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID") or "0")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
BUKVITSA_BOT_USERNAME = os.environ.get("BUKVITSA_BOT_USERNAME", "")

ZOOM_CLIENT_ID = os.environ.get("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.environ.get("ZOOM_CLIENT_SECRET", "")
ZOOM_ACCOUNT_ID = os.environ.get("ZOOM_ACCOUNT_ID", "")
ZOOM_USER_EMAIL = os.environ.get("ZOOM_USER_EMAIL", "")

# --- Провайдеры ---
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "auto")  # auto | claude | ollama
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
AUTO_ROUTING_THRESHOLD = 10000  # символов — выше → Claude, ниже → Ollama
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

TRANSCRIPTION_PROVIDER = os.environ.get("TRANSCRIPTION_PROVIDER", "bukvitsa")  # bukvitsa | whisper
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")

ALLOWED_EXTENSIONS = {".mp4", ".mp3", ".wav", ".m4a", ".webm", ".ogg"}
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
