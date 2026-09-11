"""Document upload.

The handler does HTTP concerns only — reading the upload, mapping failures to
status codes — and hands the work to `app.ingestion`.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import ingestion
from app.config import Settings, get_settings
from app.db.session import get_session
from app.media import SUPPORTED_MEDIA_TYPE_NAMES, UnsupportedMediaType
from app.models import DocumentStatus

router = APIRouter(tags=["documents"])

# Deliberately not an allow-list of known types: adding a document type must
# not require editing the pipeline (see the architecture rules in README.md).
DOC_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

READ_CHUNK_BYTES = 1024 * 1024


class DocumentResponse(BaseModel):
    id: uuid.UUID
    filename: str
    doc_type: str
    status: DocumentStatus
    page_count: int | None
    uploaded_at: datetime
    error: str | None

    model_config = {"from_attributes": True}


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    """Read the upload, refusing anything over `limit` without buffering it all."""
    chunks: list[bytes] = []
    total = 0

    while chunk := await upload.read(READ_CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds the {limit} byte upload limit.",
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _normalise_doc_type(doc_type: str) -> str:
    normalised = doc_type.strip().lower()
    if not DOC_TYPE_PATTERN.match(normalised):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "doc_type must be 1-64 characters of lowercase letters, digits, "
                "hyphens or underscores (for example 'invoice')."
            ),
        )
    return normalised


@router.post(
    "/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a PDF or image for extraction",
    description=(
        "Accepts " + ", ".join(SUPPORTED_MEDIA_TYPE_NAMES) + ". The file is stored "
        "and its pages are rendered in the background; poll the document to see "
        "page_count appear."
    ),
)
async def upload_document(
    background_tasks: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File(description="PDF or image to extract from")],
    doc_type: Annotated[str, Form(description="Document type, e.g. 'invoice'")],
) -> DocumentResponse:
    data = await _read_upload(file, settings.max_upload_bytes)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Uploaded file is empty.",
        )

    try:
        document = ingestion.store_upload(
            session,
            filename=file.filename,
            doc_type=_normalise_doc_type(doc_type),
            data=data,
        )
    except UnsupportedMediaType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc

    # README non-goals rule out a worker queue; BackgroundTasks is the sanctioned
    # way to keep the request from blocking on rendering.
    background_tasks.add_task(ingestion.render_pages_in_background, document.id)

    return DocumentResponse.model_validate(document)
