"""Document upload.

The handler does HTTP concerns only — reading the upload, mapping failures to
status codes — and hands the work to `app.ingestion`.
"""

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import export as export_service
from app import extraction, ingestion, rendering, storage
from app import media as media_types
from app.api.demo_guard import require_mutation_allowed
from app.api.rate_limit import rate_limit_demo_extraction
from app.config import Settings, get_settings
from app.db.session import get_session
from app.extractors import (
    UnknownDocumentType,
    get_extractor,
    registered_doc_types,
)
from app.media import SUPPORTED_MEDIA_TYPE_NAMES, UnsupportedMediaType
from app.models import Document, DocumentStatus
from app.providers import ExtractionProvider, get_provider

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


class ExtractionSummary(BaseModel):
    """One extraction attempt, as far as a caller needs to see it (never the raw response)."""

    id: uuid.UUID
    model_name: str
    prompt_version: str
    created_at: datetime
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    latency_ms: int | None

    model_config = {"from_attributes": True}


class DocumentDetail(DocumentResponse):
    """A document and where it stands: its status, why it failed if it did, and its latest attempt."""

    extraction_running: bool
    extraction_attempts: int
    latest_extraction: ExtractionSummary | None


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
        "and its pages are rendered in the background; poll `GET /documents/{id}` to see "
        "`page_count` appear."
    ),
    dependencies=[Depends(require_mutation_allowed)],
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


def _get_document_or_404(session: Session, document_id: uuid.UUID) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No document with id {document_id}.",
        )
    return document


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetail,
    summary="A document's status, page count and, if it failed, why",
    description=(
        "Poll this after an upload (until `page_count` appears) and after `POST .../extract` (until "
        "`status` is `extracted`, `needs_review` or `failed`). A failed document carries the reason in "
        "`error`; `latest_extraction` is the most recent attempt, successful or not."
    ),
)
def get_document(
    document_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> DocumentDetail:
    document = _get_document_or_404(session, document_id)
    attempts = list(document.extractions)
    # Read the status after the attempts: an extraction commits together with the document's new status, so
    # reading the document first could pair a new attempt with the old status.
    session.refresh(document)
    latest = attempts[-1] if attempts else None
    return DocumentDetail(
        id=document.id,
        filename=document.filename,
        doc_type=document.doc_type,
        status=document.status,
        page_count=document.page_count,
        uploaded_at=document.uploaded_at,
        error=document.error,
        extraction_running=extraction.is_extracting(document.id),
        extraction_attempts=len(attempts),
        latest_extraction=ExtractionSummary.model_validate(latest) if latest else None,
    )


@router.get(
    "/documents/{document_id}/pages/{page_number}",
    summary="A rendered page of the document, as a PNG",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}}}},
)
def get_page_image(
    document_id: uuid.UUID,
    page_number: Annotated[int, Path(ge=1)],
    session: Annotated[Session, Depends(get_session)],
) -> FileResponse:
    document = _get_document_or_404(session, document_id)
    if document.page_count is None or page_number > document.page_count:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} has no page {page_number}.",
        )
    image = rendering.page_path(storage.pages_dir(document.id), page_number)
    if not image.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"The image of page {page_number} is missing from storage.",
        )
    return FileResponse(image, media_type="image/png")


@router.get(
    "/documents/{document_id}/source",
    summary="The file as it was uploaded",
    response_class=FileResponse,
)
def get_source_file(
    document_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> FileResponse:
    document = _get_document_or_404(session, document_id)
    source = storage.resolve(document.storage_path)
    if not source.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The uploaded file is missing from storage.",
        )
    with source.open("rb") as handle:
        media = media_types.detect(handle.read(media_types.SNIFF_BYTES))
    return FileResponse(source, media_type=media.name, filename=document.filename)


class ExtractionAcceptedResponse(BaseModel):
    """What the caller gets back when an extraction has been queued."""

    document_id: uuid.UUID
    doc_type: str
    status: DocumentStatus
    page_count: int | None


@router.post(
    "/documents/{document_id}/extract",
    response_model=ExtractionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run extraction over a document's rendered pages",
    description=(
        "Sends the rendered pages to the extraction provider in the background. "
        "Accepted document types: " + ", ".join(registered_doc_types()) + "."
    ),
    dependencies=[Depends(rate_limit_demo_extraction)],
)
def extract_document(
    document_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    # `Depends(get_provider)` rather than calling `get_provider()` directly:
    # resolves to the exact same cached Anthropic provider for every normal
    # request (no behaviour change), but lets a *different* FastAPI app
    # instance override it via `app.dependency_overrides[get_provider]` --
    # the same seam `demo/app.py` uses, and nowhere else does.
    provider: Annotated[ExtractionProvider, Depends(get_provider)],
) -> ExtractionAcceptedResponse:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No document with id {document_id}.",
        )

    # Checked here so an unknown type is a 422 rather than a background failure
    # the caller never sees.
    try:
        get_extractor(document.doc_type)
    except UnknownDocumentType as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if document.page_count is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Document has no rendered pages yet; wait for rendering to "
                "finish before extracting."
            ),
        )

    if not extraction.begin_extraction(document.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An extraction for this document is already running.",
        )
    background_tasks.add_task(
        extraction.extract_document_in_background, document.id, provider=provider
    )

    return ExtractionAcceptedResponse(
        document_id=document.id,
        doc_type=document.doc_type,
        status=document.status,
        page_count=document.page_count,
    )


@router.get(
    "/documents/{document_id}/export",
    summary="Export the document's current record",
    description=(
        "The record after review: a corrected field exports the human's value, "
        "an uncorrected one the model's. Raw model responses are not included "
        "— they stay in the extraction row for debugging and evals."
    ),
    responses={
        200: {
            "content": {"application/json": {}, "text/csv": {}},
            "description": "The current record in the requested format.",
        }
    },
)
def export_document(
    document_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    format: Annotated[
        str, Query(description="json or csv")
    ] = export_service.JSON,
) -> Response:
    requested = format.strip().lower()
    if requested not in export_service.SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Unsupported export format {format!r}. Supported: "
                + ", ".join(export_service.SUPPORTED_FORMATS)
                + "."
            ),
        )

    document = export_service.get_document(session, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No document with id {document_id}.",
        )

    try:
        record = export_service.build_record(session, document)
    except export_service.ExportError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    if requested == export_service.CSV:
        return PlainTextResponse(
            export_service.to_csv(record),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{document.id}.csv"'
                )
            },
        )

    return JSONResponse(record)
