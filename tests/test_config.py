import pytest

from app.config import Settings, get_settings


def test_database_url_defaults_to_the_psycopg_driver() -> None:
    assert Settings().database_url.startswith("postgresql+psycopg://")


def test_database_url_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "postgresql+psycopg://someone:secret@db.example:5432/other"
    monkeypatch.setenv("DOCINTEL_DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        assert get_settings().database_url == url
    finally:
        get_settings.cache_clear()


def test_settings_are_cached(settings_env: None) -> None:
    assert get_settings() is get_settings()
