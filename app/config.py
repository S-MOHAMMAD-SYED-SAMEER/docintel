"""Application configuration.

All settings come from the environment (or a local `.env`), never from
module-level constants scattered through the code. See `.env.example`.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCINTEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "DocIntel"
    environment: str = "local"
    debug: bool = False

    # Uploaded documents and rendered page images (milestone 3).
    storage_dir: Path = Path("./var/storage")

    # Wired in milestone 2.
    database_url: str = "postgresql+psycopg://docintel:docintel@localhost:5432/docintel"

    # Wired in milestone 4.
    anthropic_api_key: str = ""
    extraction_model: str = "claude-sonnet-5"

    # Wired in milestone 5: any field scoring below this is routed to review.
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance, safe to use as a FastAPI dependency."""
    return Settings()
