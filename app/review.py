"""The human review queue: reading what milestone 5 decided.

Nothing here scores anything. `needs_review`, `confidence`, `model_confidence`
and the `validation` breakdown are all written once, by `app.confidence`, when
the extraction is persisted. This module only reads those rows back and puts
them in an order a reviewer can work through — recomputing a score here would
mean the queue could disagree with the database.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Document, Extraction, FieldValue

# Signals whose absence means "did not apply", not "failed". A document with no
# text layer simply never gets a text_layer signal, and the reviewer must not
# be told the value was missing from a page that has no text to search.
TEXT_LAYER_SIGNAL = "text_layer"


@dataclass(frozen=True)
class ReviewReason:
    """Why this field is in the queue, assembled from what was persisted."""

    failed_checks: tuple[str, ...]
    text_layer_miss: bool
    confidence: float

    @property
    def summary(self) -> str:
        parts: list[str] = []
        if self.failed_checks:
            parts.append(
                "deterministic checks failed: " + ", ".join(self.failed_checks)
            )
        if self.text_layer_miss:
            parts.append("the value was not found in the page text layer")
        if not parts:
            return (
                f"Final confidence {self.confidence:.2f} is below the review "
                "threshold."
            )
        return f"Final confidence {self.confidence:.2f}; " + "; ".join(parts) + "."


def signals(field_value: FieldValue) -> list[dict[str, Any]]:
    """The per-signal breakdown as stored. Empty for pre-scoring rows."""
    validation = field_value.validation or {}
    return list(validation.get("signals") or [])


def failed_checks(field_value: FieldValue) -> tuple[str, ...]:
    """Names of the deterministic checks that failed for this field."""
    validation = field_value.validation or {}
    return tuple(validation.get("blocking_failures") or [])


def text_layer_missed(field_value: FieldValue) -> bool:
    """Did a text-layer check run for this field and come back empty?

    False when the signal is absent — that is milestone 5 correctly leaving it
    out (no text layer, or a value that cannot be looked for), not a miss.
    """
    return any(
        signal.get("name") == TEXT_LAYER_SIGNAL and signal.get("score") == 0
        for signal in signals(field_value)
    )


def reason_for(field_value: FieldValue) -> ReviewReason:
    return ReviewReason(
        failed_checks=failed_checks(field_value),
        text_layer_miss=text_layer_missed(field_value),
        confidence=field_value.confidence,
    )


def _with_relations(statement):
    """Load the extraction and its document up front; the queue shows both."""
    return statement.options(
        selectinload(FieldValue.extraction).selectinload(Extraction.document)
    )


def queue(session: Session, *, limit: int = 50, offset: int = 0) -> list[FieldValue]:
    """Fields awaiting review, least confident first.

    Ordered so a reviewer meets the worst answers first, with a stable
    tie-break so the same queue pages the same way twice.
    """
    statement = (
        _with_relations(select(FieldValue))
        .where(FieldValue.needs_review.is_(True))
        .order_by(
            FieldValue.confidence.asc(),
            FieldValue.field_name.asc(),
            FieldValue.id.asc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(statement).scalars().all())


def queue_size(session: Session) -> int:
    """How many fields are waiting, regardless of the page being shown."""
    statement = (
        select(func.count())
        .select_from(FieldValue)
        .where(FieldValue.needs_review.is_(True))
    )
    return session.execute(statement).scalar_one()


def get_field(session: Session, field_id: uuid.UUID) -> FieldValue | None:
    statement = _with_relations(select(FieldValue)).where(FieldValue.id == field_id)
    return session.execute(statement).scalar_one_or_none()


def document_of(field_value: FieldValue) -> Document:
    return field_value.extraction.document
