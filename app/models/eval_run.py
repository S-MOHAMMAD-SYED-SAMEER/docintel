"""The `eval_runs` table: one row per evaluation of a dataset.

Every prompt change bumps `prompt_version` and gets a new row, so a change to
the prompt always has a before/after number attached to it.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Float, Integer, Numeric, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    dataset_name: Mapped[str] = mapped_column(String(128), index=True)
    prompt_version: Mapped[str] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(128))

    document_count: Mapped[int] = mapped_column(Integer)
    # field name -> {evaluated, correct, accuracy}
    field_accuracy: Mapped[dict[str, Any]] = mapped_column(JSONB)

    # The two rates the README names, as columns because they are what a run is
    # read for. Everything else — percentiles, outcome counts, failures — lives
    # in `metrics` rather than sprawling into more columns.
    review_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    false_confident_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    mean_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    mean_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<EvalRun {self.dataset_name} {self.prompt_version} "
            f"{self.model_name} at {self.created_at}>"
        )
