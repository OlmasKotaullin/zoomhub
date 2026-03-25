from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from app.config import DATABASE_URL, DATA_DIR, RECORDINGS_DIR, LOGS_DIR

_is_sqlite = DATABASE_URL.startswith("sqlite")


class Base(DeclarativeBase):
    pass


# Engine: SQLite vs PostgreSQL
_engine_kwargs: dict = {}

if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 60}
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs["poolclass"] = QueuePool
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20

engine = create_engine(DATABASE_URL, **_engine_kwargs)


# WAL mode for SQLite only
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    import app.models  # noqa: F401 — register models
    Base.metadata.create_all(bind=engine)

    # Auto-migrate: add columns that may be missing in existing PostgreSQL tables
    if not _is_sqlite:
        from sqlalchemy import text, inspect
        insp = inspect(engine)
        with engine.connect() as c:
            user_cols = {col["name"] for col in insp.get_columns("users")}
            meeting_cols = {col["name"] for col in insp.get_columns("meetings")}

            migrations = []
            if "zoom_access_token" not in user_cols:
                migrations += [
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS zoom_access_token TEXT",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS zoom_refresh_token TEXT",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS zoom_token_expires_at TIMESTAMP",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS zoom_user_email VARCHAR(255)",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id VARCHAR(100)",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_telegram BOOLEAN DEFAULT FALSE",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_email BOOLEAN DEFAULT FALSE",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS capture_source VARCHAR(20) DEFAULT 'both'",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS agent_api_token VARCHAR(500)",
                ]
            if "zoom_recording_id" not in meeting_cols:
                migrations.append("ALTER TABLE meetings ADD COLUMN IF NOT EXISTS zoom_recording_id VARCHAR(255) UNIQUE")

            if "onboarding_completed" not in user_cols:
                migrations += [
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_code_id INTEGER",
                ]

            if "is_admin" not in user_cols:
                migrations += [
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE",
                    "UPDATE users SET is_admin = TRUE WHERE email = 'annggeellooss@gmail.com'",
                ]

            if "user_groq_api_key" not in user_cols:
                migrations += [
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS user_groq_api_key VARCHAR(500)",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS user_gemini_api_key VARCHAR(500)",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS user_gigachat_auth_key VARCHAR(1000)",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS user_anthropic_api_key VARCHAR(500)",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS user_llm_provider VARCHAR(50) DEFAULT 'auto'",
                ]

            # Chat system: session_id and tokens_used on chat_messages
            # Note: chat_sessions table is created by create_all() above, so FK is safe
            if insp.has_table("chat_messages") and insp.has_table("chat_sessions"):
                chat_cols = {col["name"] for col in insp.get_columns("chat_messages")}
                if "session_id" not in chat_cols:
                    migrations += [
                        "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS session_id INTEGER REFERENCES chat_sessions(id)",
                        "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS tokens_used INTEGER",
                    ]

            for sql in migrations:
                c.execute(text(sql))
            if migrations:
                c.commit()
