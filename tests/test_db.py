import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.base import Base
from app.db.session import get_engine, get_session, reset_engine


def test_engine_uses_the_configured_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DOCINTEL_DATABASE_URL",
        "postgresql+psycopg://someone:secret@db.example:5432/other",
    )
    get_settings.cache_clear()
    reset_engine()
    try:
        url = get_engine().url
        assert url.drivername == "postgresql+psycopg"
        assert url.host == "db.example"
        assert url.database == "other"
    finally:
        get_settings.cache_clear()
        reset_engine()


def test_engine_is_reused(settings_env: None) -> None:
    assert get_engine() is get_engine()


def test_get_session_yields_a_usable_session(migrated_engine: object) -> None:
    sessions = get_session()
    db_session = next(sessions)
    try:
        assert db_session.execute(text("select 1")).scalar_one() == 1
    finally:
        sessions.close()


def test_constraint_naming_convention_is_configured() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
