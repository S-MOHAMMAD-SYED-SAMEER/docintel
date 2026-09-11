"""The server-rendered review page.

Served outside the /api/v1 prefix: this is the reviewer's page, not part of the
JSON contract. It reads the same persisted rows the API does — the page never
scores anything of its own.
"""

from itertools import groupby
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import review
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
        },
    )
