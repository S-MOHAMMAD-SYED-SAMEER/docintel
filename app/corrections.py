"""Applying a human correction, and resolving what it means for the document.

A correction is ground truth. Nothing here re-runs the model, re-scores the
field, or asks the provider anything — the human has settled it.

What changes:

    field_values.value          becomes the corrected value
    field_values.needs_review   becomes false; the field has been handled
    corrections                 gains a row holding the superseded value

What deliberately does not change:

    field_values.model_confidence   what the model claimed, kept verbatim
    field_values.confidence         the score for the model's value, frozen
    field_values.validation         the signals that produced that score
    extractions.raw_response        the model's answer, kept verbatim

Those four describe the *model's* extraction and stay true of it forever.
Read them alongside `FieldValue.is_corrected`, which says whether the current
value is still the one they describe.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Correction, Document, DocumentStatus, Extraction, FieldValue

logger = logging.getLogger(__name__)

# A correction can only settle a document that was waiting on a human. A failed
# or still-processing document is not "reviewed" because someone edited a field.
RESOLVABLE_STATUSES = (DocumentStatus.NEEDS_REVIEW, DocumentStatus.EXTRACTED)


class CorrectionError(Exception):
    """The correction could not be applied."""


def apply_correction(
    session: Session, field_value: FieldValue, corrected_value: str | None
) -> Correction:
    """Record the correction, update the field, and resolve the document.

    `original_value` is whatever the field held a moment ago — the model's
    answer on the first correction, the previous correction's value after
    that — so repeated corrections chain back to the original without any of
    them being overwritten.
    """
    correction = Correction(
        field_value_id=field_value.id,
        original_value=field_value.value,
        corrected_value=corrected_value,
    )
    session.add(correction)

    field_value.value = corrected_value
    field_value.needs_review = False

    document = field_value.extraction.document
    resolve_document_status(session, document)

    session.commit()
    session.refresh(correction)

    logger.info(
        "corrected %s on document %s: %r -> %r (document now %s)",
        field_value.field_name,
        document.id,
        correction.original_value,
        correction.corrected_value,
        document.status,
    )
    return correction


def outstanding_review_count(session: Session, document: Document) -> int:
    """Fields of this document that still need a human."""
    statement = (
        select(FieldValue)
        .join(Extraction, FieldValue.extraction_id == Extraction.id)
        .where(Extraction.document_id == document.id)
        .where(FieldValue.needs_review.is_(True))
    )
    return len(session.execute(statement).scalars().all())


def resolve_document_status(session: Session, document: Document) -> DocumentStatus:
    """Move the document to `reviewed` only once nothing is outstanding.

    Flushes first so a field just marked handled in this session is counted.
    """
    if document.status not in RESOLVABLE_STATUSES:
        return document.status

    session.flush()
    if outstanding_review_count(session, document) == 0:
        document.status = DocumentStatus.REVIEWED
    else:
        document.status = DocumentStatus.NEEDS_REVIEW

    return document.status


def history(session: Session, field_value_id: uuid.UUID) -> list[Correction]:
    """Every correction made to a field, oldest first."""
    statement = (
        select(Correction)
        .where(Correction.field_value_id == field_value_id)
        .order_by(Correction.corrected_at.asc(), Correction.id.asc())
    )
    return list(session.execute(statement).scalars().all())


def recent(session: Session, *, limit: int = 10) -> list[Correction]:
    """The newest corrections, so a reviewer can see their work was recorded."""
    statement = (
        select(Correction)
        .order_by(Correction.corrected_at.desc(), Correction.id.desc())
        .limit(limit)
        .options(
            selectinload(Correction.field_value)
            .selectinload(FieldValue.extraction)
            .selectinload(Extraction.document)
        )
    )
    return list(session.execute(statement).scalars().all())
