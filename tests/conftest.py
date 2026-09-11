import io
import os
from collections.abc import Iterator
from pathlib import Path

import pypdfium2 as pdfium
import pytest
import sqlalchemy
from PIL import Image
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import reset_engine
from app.main import create_app

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tests never touch the development database. Override with
# DOCINTEL_TEST_DATABASE_URL to point at a different server.
TEST_DATABASE_URL = os.environ.get(
    "DOCINTEL_TEST_DATABASE_URL",
    "postgresql+psycopg://docintel:docintel@localhost:5432/docintel_test",
)


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give a test a clean settings/engine cache and restore it afterwards."""
    monkeypatch.setenv("DOCINTEL_ENVIRONMENT", "test")
    get_settings.cache_clear()
    reset_engine()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine()


@pytest.fixture
def client(settings_env: None) -> Iterator[TestClient]:
    yield TestClient(create_app())


@pytest.fixture(scope="session")
def database_url() -> str:
    """The test database URL, skipping the test if no server is reachable."""
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect():
            pass
    except sqlalchemy.exc.OperationalError as exc:
        pytest.skip(f"no PostgreSQL at {TEST_DATABASE_URL}: {exc}")
    finally:
        engine.dispose()
    return TEST_DATABASE_URL


@pytest.fixture
def alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(os.path.join(PROJECT_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(PROJECT_ROOT, "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def migrated_engine(
    database_url: str,
    alembic_config: AlembicConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    """A database migrated to head, torn back down to empty afterwards.

    Running the real migration rather than `Base.metadata.create_all` is the
    point: it is the migration, not the model, that has to work on a fresh
    database.
    """
    from alembic import command

    # env.py reads the URL from settings, so the settings must point at the
    # test database for the duration.
    monkeypatch.setenv("DOCINTEL_DATABASE_URL", database_url)
    get_settings.cache_clear()
    reset_engine()

    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(alembic_config, "base")
        get_settings.cache_clear()
        reset_engine()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    with Session(migrated_engine) as db_session:
        yield db_session


@pytest.fixture
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point storage at a throwaway directory for the duration of a test."""
    root = tmp_path / "storage"
    monkeypatch.setenv("DOCINTEL_STORAGE_DIR", str(root))
    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


@pytest.fixture
def api_client(
    migrated_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A client backed by the migrated test database and temporary storage."""
    monkeypatch.setenv("DOCINTEL_ENVIRONMENT", "test")
    get_settings.cache_clear()
    reset_engine()
    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()
        reset_engine()


def build_pdf(pages: int = 3) -> bytes:
    """A real, minimal PDF — rendering is the thing under test, so no stubs."""
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 260)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def build_png(size: tuple[int, int] = (120, 80)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (200, 30, 30, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def pdf_bytes() -> bytes:
    return build_pdf()


@pytest.fixture
def png_bytes() -> bytes:
    return build_png()
