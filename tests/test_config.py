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


def test_the_key_is_read_from_a_dotenv_under_either_name(tmp_path, monkeypatch) -> None:
    """A `.env` holding the conventional `ANTHROPIC_API_KEY` works; `DOCINTEL_ANTHROPIC_API_KEY` wins when both are set."""
    from app.config import Settings

    for name in ("ANTHROPIC_API_KEY", "DOCINTEL_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    conventional = tmp_path / "a.env"
    conventional.write_text("ANTHROPIC_API_KEY=placeholder-one\n")
    assert Settings(_env_file=conventional).anthropic_api_key == "placeholder-one"

    both = tmp_path / "b.env"
    both.write_text("ANTHROPIC_API_KEY=placeholder-one\nDOCINTEL_ANTHROPIC_API_KEY=placeholder-two\n")
    assert Settings(_env_file=both).anthropic_api_key == "placeholder-two"

    assert Settings(_env_file=None).anthropic_api_key == ""


def test_the_key_can_come_from_the_process_environment_under_either_name(monkeypatch) -> None:
    from app.config import Settings

    monkeypatch.delenv("DOCINTEL_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-env")
    assert Settings(_env_file=None).anthropic_api_key == "placeholder-env"

    monkeypatch.setenv("DOCINTEL_ANTHROPIC_API_KEY", "placeholder-docintel")
    assert Settings(_env_file=None).anthropic_api_key == "placeholder-docintel"
