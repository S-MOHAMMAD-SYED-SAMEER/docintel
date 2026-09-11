"""The upload → storage → page render leg of the pipeline.

The route handler does HTTP; this module owns the order of operations and the
status transitions, so the same flow can be driven from a test or a script
without going through FastAPI.

Status, in this milestone:

    UPLOADED   bytes are on disk, nothing rendered yet
    PROCESSING pages are being rendered, and — once they are — the document is
               waiting on the extractor, which arrives in milestone 4
    FAILED     rendering gave up; `error` says why and the source file is kept

A rendered document deliberately stays PROCESSING rather than moving to
EXTRACTED: nothing has been extracted yet, and saying otherwise would be a lie
the review queue would later have to untangle.
"""

import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import media as media_types
from app import rendering, storage
from app.db.session import get_sessionmaker
from app.media import MediaType, UnsupportedMediaType
from app.models import Document, DocumentStatus

logger = logging.getLogger(__name__)


def store_upload(
    session: Session, *, filename: str | None, doc_type: str, data: bytes
) -> Document:
    """Validate the bytes, write them to disk, and create the document row.

    Raises `UnsupportedMediaType` before anything is written.
    """
    media = media_types.detect(data)
    document_id = uuid.uuid4()

    stored = storage.save_source(document_id, data, media)
    document = Document(
        id=document_id,
        filename=storage.safe_filename(filename),
        storage_path=stored.relative_path,
        doc_type=doc_type,
        status=DocumentStatus.UPLOADED,
    )
    session.add(document)
    try:
        session.commit()
    except Exception:
        # Never leave bytes on disk that no row points at.
        session.rollback()
        storage.delete_document_files(document_id)
        raise

    session.refresh(document)
    logger.info("stored document %s (%s)", document_id, media.name)
    return document


def render_pages(session: Session, document: Document) -> Document:
    """Render the stored file to page images and record the outcome.

    Failures are persisted against the document rather than raised: a document
    that cannot be rendered is a FAILED document with a stored reason, not a
    lost one.
    """
    document.status = DocumentStatus.PROCESSING
    document.error = None
    session.commit()

    source = storage.resolve(document.storage_path)
    try:
        media = _media_for(source)
        pages = rendering.render(document.id, source, media)
    except (rendering.RenderError, UnsupportedMediaType, OSError) as exc:
        logger.warning("render failed for document %s: %s", document.id, exc)
        document.status = DocumentStatus.FAILED
        document.error = str(exc)
        session.commit()
        return document

    document.page_count = len(pages)
    session.commit()
    logger.info("rendered %d page(s) for document %s", len(pages), document.id)
    return document


def render_pages_in_background(document_id: uuid.UUID) -> None:
    """Entry point for `BackgroundTasks`; owns its own session.

    The request's session is gone by the time this runs, so it opens a fresh
    one. Any unexpected error is logged and stored — a background task that
    dies quietly would strand the document in PROCESSING for ever.
    """
    with get_sessionmaker()() as session:
        document = session.get(Document, document_id)
        if document is None:
            logger.error("document %s vanished before rendering", document_id)
            return

        try:
            render_pages(session, document)
        except Exception as exc:  # noqa: BLE001 - last line of defence
            logger.exception("unexpected render failure for document %s", document_id)
            session.rollback()
            document.status = DocumentStatus.FAILED
            document.error = f"Unexpected rendering failure: {exc}"
            session.commit()


def _media_for(source: Path) -> MediaType:
    """Re-detect the media type from the stored bytes.

    The upload was checked before it was written, but the renderer works from
    what is on disk now, so that is what gets inspected.
    """
    if not source.is_file():
        raise rendering.RenderError(f"Stored file is missing: {source}")

    with source.open("rb") as handle:
        return media_types.detect(handle.read(media_types.SNIFF_BYTES))
