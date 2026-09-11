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

    # Root for uploaded documents and their rendered page images.
    storage_dir: Path = Path("./var/storage")
    # Uploads larger than this are rejected with 413 before anything is written.
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    # Render resolution for PDF pages. Higher is sharper but costs more to send
    # to a vision model later.
    render_dpi: int = Field(default=200, ge=72, le=600)

    database_url: str = "postgresql+psycopg://docintel:docintel@localhost:5432/docintel"

    # Extraction provider.
    anthropic_api_key: str = ""
    extraction_model: str = "claude-sonnet-5"
    # Ceiling for the extraction response. An invoice schema with line items is
    # a few thousand tokens; the headroom is for long line-item tables.
    extraction_max_tokens: int = Field(default=16000, gt=0)

    # Wired in milestone 5: any field scoring below this is routed to review.
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance, safe to use as a FastAPI dependency."""
    return Settings()
