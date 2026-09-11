"""The review queue API.

Read-only in this milestone. Everything the reviewer sees was decided and
persisted when the extraction ran, so the queue and the database can never
disagree about whether a field needs review.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import review
from app.config import Settings, get_settings
from app.db.session import get_session
from app.models import DocumentStatus, FieldValue

router = APIRouter(tags=["review"])

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class ReviewDocument(BaseModel):
    id: uuid.UUID
    filename: str
    doc_type: str
    status: DocumentStatus
    page_count: int | None


class ReviewSignal(BaseModel):
    """One contribution to the field's score, exactly as it was recorded."""

    name: str
    weight: float
    score: float
    detail: str = ""


class ReviewField(BaseModel):
    field_id: uuid.UUID
    field_name: str
    value: str | None
    # The final combined score — what `needs_review` was decided on.
    confidence: float
    # What the model claimed, kept separately so a reviewer can see the gap.
    model_confidence: float
    source_page: int | None
    needs_review: bool
    failed_checks: list[str]
    text_layer_miss: bool
    signals: list[ReviewSignal]
    review_reason: str
    document: ReviewDocument
    extraction_id: uuid.UUID
    model_name: str
    prompt_version: str


class ReviewQueue(BaseModel):
    total: int = Field(description="Fields awaiting review in total.")
    limit: int
    offset: int
    # Echoed so a caller can see the threshold these rows were judged against.
    confidence_threshold: float
    items: list[ReviewField]


def to_review_field(field_value: FieldValue) -> ReviewField:
    document = review.document_of(field_value)
    extraction = field_value.extraction
    reason = review.reason_for(field_value)

    return ReviewField(
        field_id=field_value.id,
        field_name=field_value.field_name,
        value=field_value.value,
        confidence=field_value.confidence,
        model_confidence=field_value.model_confidence,
        source_page=field_value.source_page,
        needs_review=field_value.needs_review,
        failed_checks=list(reason.failed_checks),
        text_layer_miss=reason.text_layer_miss,
        signals=[ReviewSignal(**signal) for signal in review.signals(field_value)],
        review_reason=reason.summary,
        document=ReviewDocument(
            id=document.id,
            filename=document.filename,
            doc_type=document.doc_type,
            status=document.status,
            page_count=document.page_count,
        ),
        extraction_id=extraction.id,
        model_name=extraction.model_name,
        prompt_version=extraction.prompt_version,
    )


@router.get(
    "/review",
    response_model=ReviewQueue,
    summary="Fields awaiting human review",
    description=(
        "Only fields whose persisted needs_review is true, least confident "
        "first. Confidence and validation come from the extraction that wrote "
        "them; nothing is rescored here."
    ),
)
def review_queue(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewQueue:
    field_values = review.queue(session, limit=limit, offset=offset)

    return ReviewQueue(
        total=review.queue_size(session),
        limit=limit,
        offset=offset,
        confidence_threshold=settings.confidence_threshold,
        items=[to_review_field(field_value) for field_value in field_values],
    )


@router.post(
    "/review/{field_id}",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    summary="Submit a correction for a field (not yet implemented)",
    description=(
        "Part of the README API contract. Storing corrections is milestone 7, "
        "so this route resolves the field and then declines: it never writes. "
        "The request body is deliberately unspecified here so milestone 7 can "
        "define it."
    ),
)
def submit_correction(
    field_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    field_value = review.get_field(session, field_id)
    if field_value is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No field value with id {field_id}.",
        )

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Corrections are not stored yet. The review queue is read-only in "
            "this milestone; correction persistence lands in milestone 7."
        ),
    )
