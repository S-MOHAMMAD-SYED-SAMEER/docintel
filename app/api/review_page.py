"""The server-rendered review page.

Served outside the /api/v1 prefix: this is the reviewer's page, not part of the
JSON contract. It reads the same persisted rows the API does — the page never
scores anything of its own.
"""

import uuid
from itertools import groupby
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import corrections, review
from app.api.v1.review import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ReviewField,
    to_review_field,
)
from app.config import Settings, get_settings
from app.db.session import get_session

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

router = APIRouter(tags=["review"], include_in_schema=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _grouped(items: list[ReviewField]) -> list[tuple[str, list[ReviewField]]]:
    """Rows grouped under their document, keeping the queue's order.

    Sorting by document first would bury the least confident fields; sorting
    within the queue order keeps the worst answers near the top while still
    letting a reviewer work a document at a time.
    """
    ordered = sorted(items, key=lambda item: str(item.document.id))
    return [
        (rows[0].document.filename, rows)
        for _, group in groupby(ordered, key=lambda item: str(item.document.id))
        if (rows := list(group))
    ]


@router.get("/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    items = [
        to_review_field(field_value)
        for field_value in review.queue(session, limit=limit, offset=offset)
    ]

    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "app_name": settings.app_name,
            "total": review.queue_size(session),
            "confidence_threshold": settings.confidence_threshold,
            "items": items,
            "grouped": _grouped(items),
            "recent": corrections.recent(session),
        },
    )


@router.post("/review/{field_id}")
def submit_correction_form(
    field_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    corrected_value: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """The page's form target: form-encoded in, redirect back out.

    Separate from the JSON endpoint so the API keeps a single content type,
    and a POST-redirect-GET so a reload does not resubmit the correction. No
    JavaScript is involved.
    """
    field_value = review.get_field(session, field_id)
    if field_value is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No field value with id {field_id}.",
        )

    # An empty box means "the document does not state this", which is a
    # correction in its own right.
    value = corrected_value.strip() or None
    corrections.apply_correction(session, field_value, value)

    return RedirectResponse("/review", status_code=status.HTTP_303_SEE_OTHER)
