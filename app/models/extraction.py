"""The `extractions` and `field_values` tables.

One `extractions` row per model call, holding the response verbatim plus what
the call cost. One `field_values` row per field the model was asked for.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.document import Document


class Extraction(Base):
    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))

    # Never discarded: debugging a bad extraction and re-scoring an old run both
    # need the original response.
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Null when the response could not be parsed into the schema — the failure
    # is recorded in `error` rather than stored as an empty result.
    parsed: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Fractions of a cent per page, so six decimal places.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped["Document"] = relationship(back_populates="extractions")
    field_values: Mapped[list["FieldValue"]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        order_by="FieldValue.field_name",
    )

    def __repr__(self) -> str:
        return (
            f"<Extraction id={self.id} document_id={self.document_id} "
            f"model={self.model_name!r} prompt={self.prompt_version!r}>"
        )


class FieldValue(Base):
    __tablename__ = "field_values"
    __table_args__ = (
        # One row per field per extraction; a second row for the same field
        # would make "the value of total" ambiguous.
        UniqueConstraint("extraction_id", "field_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"),
        index=True,
    )
    field_name: Mapped[str] = mapped_column(String(128))
    # Null means the model did not find the field. Text, because a reviewer
    # corrects what the document says, not a typed value.
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The final score: the model's confidence combined with the deterministic
    # signals, per `app/confidence.py`.
    confidence: Mapped[float] = mapped_column(Float)
    # What the model said about itself, kept verbatim. Never overwritten by
    # scoring — comparing the two is how you find a model that is confidently
    # wrong, which is the metric the README cares about most.
    model_confidence: Mapped[float] = mapped_column(Float)
    # Why the field scored what it did: the signals, their weights, and any
    # deterministic check that failed. Null for rows written before scoring.
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set by scoring: below the configured threshold, or a deterministic check
    # failed. See `app/confidence.py`.
    needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    extraction: Mapped["Extraction"] = relationship(back_populates="field_values")

    def __repr__(self) -> str:
        return (
            f"<FieldValue {self.field_name}={self.value!r} "
            f"confidence={self.confidence}>"
        )
