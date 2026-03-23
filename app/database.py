from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import DB_URL, DATA_DIR, RECORDINGS_DIR, LOGS_DIR


class Base(DeclarativeBase):
    pass


# NullPool — каждая сессия получает свой connection (важно для async-задач)
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False, "timeout": 60},
    poolclass=NullPool,
)


# WAL mode для SQLite — убирает "database is locked"
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
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
