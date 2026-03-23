import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
import app.database as database_module
from app.models import Folder, Meeting, Transcript, Summary, ChatMessage  # noqa: F401

TEST_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
TestSession = sessionmaker(bind=TEST_ENGINE)


@pytest.fixture(autouse=True)
def setup_db():
    """Создаёт таблицы перед каждым тестом, удаляет после."""
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def db_session():
    """SQLAlchemy session для юнит-тестов моделей."""
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    """FastAPI TestClient с подменённой БД."""
    from fastapi.testclient import TestClient
    from app.main import app

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    # Подменяем engine и SessionLocal в модуле database
    original_engine = database_module.engine
    original_session_local = database_module.SessionLocal
    database_module.engine = TEST_ENGINE
    database_module.SessionLocal = TestSession

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()
    database_module.engine = original_engine
    database_module.SessionLocal = original_session_local
