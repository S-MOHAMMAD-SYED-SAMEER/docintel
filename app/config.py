"""Application configuration.

All settings come from the environment (or a local `.env`), never from
module-level constants scattered through the code. See `.env.example`.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
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
    # The credential. `DOCINTEL_ANTHROPIC_API_KEY` or the SDK's own `ANTHROPIC_API_KEY`, in the environment or in
    # `.env`: a `.env` that holds the conventional name works without the process having to export it first.
    # `DOCINTEL_ANTHROPIC_API_KEY` wins when both are set. Empty means the SDK's own lookup (and no key means
    # an extraction fails with a clear `ProviderAuthError`).
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("DOCINTEL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    extraction_model: str = "claude-haiku-4-5"
    # Ceiling for the extraction response. An invoice schema with line items is
    # a few thousand tokens; the headroom is for long line-item tables.
    extraction_max_tokens: int = Field(default=16000, gt=0)
    # Which invoice prompt is used: `invoice/v1` (the default) or `invoice/v2` (see docs/PROMPTS.md and docs/EVAL.md for
    # how the default is decided). The version is stored with every extraction.
    invoice_prompt_version: str = "invoice/v1"
    # How the Anthropic adapter treats a slow or failing service. Stated here, never left to the SDK's
    # defaults (a 10-minute read timeout inside a background task, two silent retries).
    # A request is cut off after this many seconds:
    extraction_timeout_seconds: float = Field(default=120.0, gt=0)
    # A transient failure (a timeout, a connection error, 408, 409, 429 or a 5xx) is retried this many more
    # times by the SDK, which waits what the service asks (Retry-After) or backs off. 0 turns retries off,
    # which is what a counted evaluation run uses. 401, 403 and other 4xx are never retried.
    extraction_max_retries: int = Field(default=2, ge=0, le=5)
    # A document with more pages than this is refused before any model call, and so is one whose rendered
    # pages together exceed the byte limit (the API takes about 100 images and 32 MB a request).
    max_pages: int = Field(default=20, ge=1, le=100)
    max_image_bytes_total: int = Field(default=20 * 1024 * 1024, gt=0)
    # USD per million tokens as `{"<model id>": [input, output]}`, to add or override entries of the table
    # in `app/providers/pricing.py`. A model with no entry has no cost recorded (None), never a guess.
    pricing_usd_per_mtok: dict[str, tuple[Decimal, Decimal]] = Field(default_factory=dict)

    # A field whose final score falls below this is routed to human review.
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # Weights for the four confidence signals. They need not sum to 1: only
    # the signals that apply to a field are used, and their weights are
    # renormalised over that subset. The defaults give the model's own opinion
    # less than half the say, so the deterministic signals together outweigh it
    # whenever they apply.
    confidence_weight_model: float = Field(default=0.40, ge=0.0)
    confidence_weight_schema: float = Field(default=0.20, ge=0.0)
    confidence_weight_arithmetic: float = Field(default=0.20, ge=0.0)
    confidence_weight_text_layer: float = Field(default=0.20, ge=0.0)

    # --- Demo rate limiting ---
    # Off by default, so normal/Live Mode behaviour is unchanged unless an
    # operator deliberately opts in for a public demo deployment. When
    # true, `POST /api/v1/documents/{id}/extract` (see
    # `app/api/rate_limit.py`) refuses a request over
    # `demo_rate_limit_per_minute` from the same client IP with a 429,
    # rather than letting an automated loop trigger extraction without
    # limit. In-process only -- no Redis, no database table.
    demo_rate_limit_enabled: bool = False
    # Conservative default for a single-replica portfolio demo: generous
    # enough for a visitor trying the three seeded invoices a few times
    # over, tight enough to stop a scripted loop quickly.
    demo_rate_limit_per_minute: int = Field(default=10, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance, safe to use as a FastAPI dependency."""
    return Settings()
