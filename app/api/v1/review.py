"""The review queue API.

Read-only in this milestone. Everything the reviewer sees was decided and
persisted when the extraction ran, so the queue and the database can never
disagree about whether a field needs review.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import corrections, review
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
    # True once a human has replaced the value. The confidence columns above
    # still describe the model's answer, not this one.
    corrected: bool
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
        corrected=field_value.is_corrected,
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


class CorrectionRequest(BaseModel):
    """What a reviewer says the document actually says."""

    corrected_value: str | None = Field(
        description=(
            "The correct value for this field. Null clears a value the model "
            "should not have produced — that is a correction too."
        )
    )


class CorrectionResponse(BaseModel):
    correction_id: uuid.UUID
    field_id: uuid.UUID
    field_name: str
    original_value: str | None
    corrected_value: str | None
    corrected_at: datetime
    needs_review: bool
    # Frozen at extraction time: both describe the model's answer, not this one.
    confidence: float
    model_confidence: float
    document_id: uuid.UUID
    document_status: DocumentStatus
    outstanding_review_fields: int


@router.post(
    "/review/{field_id}",
    response_model=CorrectionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a correction for a field",
    description=(
        "Records the correction, makes the corrected value the field's current "
        "value, and clears its review flag. The model's confidence, score and "
        "validation are left untouched — they describe the answer that was "
        "superseded. The model is not called again."
    ),
)
def submit_correction(
    field_id: uuid.UUID,
    payload: CorrectionRequest,
    session: Annotated[Session, Depends(get_session)],
) -> CorrectionResponse:
    field_value = review.get_field(session, field_id)
    if field_value is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No field value with id {field_id}.",
        )

    correction = corrections.apply_correction(
        session, field_value, payload.corrected_value
    )
    document = field_value.extraction.document

    return CorrectionResponse(
        correction_id=correction.id,
        field_id=field_value.id,
        field_name=field_value.field_name,
        original_value=correction.original_value,
        corrected_value=correction.corrected_value,
        corrected_at=correction.corrected_at,
        needs_review=field_value.needs_review,
        confidence=field_value.confidence,
        model_confidence=field_value.model_confidence,
        document_id=document.id,
        document_status=document.status,
        outstanding_review_fields=corrections.outstanding_review_count(
            session, document
        ),
    )
