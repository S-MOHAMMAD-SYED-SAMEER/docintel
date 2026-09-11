"""The extractor leg of the pipeline: rendered pages in, stored fields out.

Sits beside `app.ingestion` and follows the same shape — the route handler does
HTTP, this module owns the order of operations and the status transitions.

    PROCESSING  pages rendered, extraction running
    EXTRACTED   the model answered and the answer parsed into the schema
    FAILED      the provider or the parse gave up; `error` says why

Nothing here decides whether a field needs review. Confidence is stored exactly
as the model reported it; combining it with deterministic checks, and the
threshold that routes a field to the queue, are milestone 5.
"""

import json
import logging
import mimetypes
import uuid
from collections.abc import Iterator

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app import storage
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.extractors import UnknownDocumentType, get_extractor
from app.extractors.base import ExtractedField, Extractor
from app.models import Document, DocumentStatus, Extraction, FieldValue
from app.providers import ExtractionProvider, PageImage, ProviderError, get_provider

logger = logging.getLogger(__name__)

DEFAULT_PAGE_MEDIA_TYPE = "image/png"


class ExtractionError(Exception):
    """Extraction could not be attempted or completed."""


def load_page_images(document: Document) -> list[PageImage]:
    """Read the document's rendered pages, in page order."""
    pages_dir = storage.pages_dir(document.id)
    if not pages_dir.is_dir():
        raise ExtractionError(
            f"Document {document.id} has no rendered pages; render it first."
        )

    images = [
        PageImage(
            page_number=number,
            media_type=mimetypes.guess_type(path.name)[0] or DEFAULT_PAGE_MEDIA_TYPE,
            data=path.read_bytes(),
        )
        for number, path in enumerate(sorted(pages_dir.glob("page-*.png")), start=1)
    ]
    if not images:
        raise ExtractionError(f"Document {document.id} has no page images.")
    return images


def extract_document(
    session: Session,
    document: Document,
    provider: ExtractionProvider | None = None,
) -> Extraction:
    """Run one extraction and persist everything it produced.

    An `Extraction` row is always written, successful or not — the raw response
    is the record of what the model actually said, and a failure with a stored
    reason is worth more than no row at all.
    """
    extractor = get_extractor(document.doc_type)
    images = load_page_images(document)
    # Resolved only once the call is definitely happening, so a document that
    # cannot be extracted never builds a provider (or its API client).
    provider = provider or get_provider()

    document.status = DocumentStatus.PROCESSING
    document.error = None
    session.commit()

    try:
        raw = provider.extract(images, extractor.schema, extractor.prompt)
    except ProviderError as exc:
        logger.warning("provider failed for document %s: %s", document.id, exc)
        return _record_provider_failure(session, document, extractor, provider, exc)

    extraction = Extraction(
        document_id=document.id,
        model_name=raw.model_name,
        prompt_version=extractor.prompt_version,
        raw_response=raw.raw_response,
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
        cost_usd=raw.cost_usd,
        latency_ms=raw.latency_ms,
    )

    try:
        parsed = parse_response(raw.content, extractor.schema)
    except ExtractionError as exc:
        # The raw response is still stored: an unparseable answer is exactly
        # what you need to look at when fixing the prompt.
        extraction.error = str(exc)
        document.status = DocumentStatus.FAILED
        document.error = str(exc)
        session.add(extraction)
        session.commit()
        logger.warning("could not parse extraction for %s: %s", document.id, exc)
        return extraction

    extraction.parsed = parsed.model_dump(mode="json")
    extraction.field_values = list(build_field_values(parsed))

    document.status = DocumentStatus.EXTRACTED
    document.error = None
    session.add(extraction)
    session.commit()
    session.refresh(extraction)

    logger.info(
        "extracted document %s with %s (%s): %d field(s), %s USD",
        document.id,
        raw.model_name,
        extractor.prompt_version,
        len(extraction.field_values),
        raw.cost_usd,
    )
    return extraction


def parse_response(content: str, schema: type[BaseModel]) -> BaseModel:
    """Parse the model's text into the extractor's schema, or fail loudly."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"Model response was not valid JSON: {exc}") from exc

    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise ExtractionError(
            f"Model response did not match {schema.__name__}: "
            f"{exc.error_count()} validation error(s); {exc}"
        ) from exc


def build_field_values(parsed: BaseModel) -> Iterator[FieldValue]:
    """One row per top-level field of the schema.

    Every field of an extraction schema is an `ExtractedField`, so each one
    carries its own confidence and source page. A structured value (the
    line-item table) is stored as JSON text — `field_values.value` is text
    because a reviewer corrects what the document says.
    """
    for field_name in type(parsed).model_fields:
        field = getattr(parsed, field_name)
        if not isinstance(field, ExtractedField):
            raise ExtractionError(
                f"Schema field {field_name!r} is not an ExtractedField; "
                "extraction schemas must report confidence per field."
            )

        yield FieldValue(
            field_name=field_name,
            value=_as_text(field.value),
            confidence=field.confidence,
            source_page=field.source_page,
        )


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if isinstance(value, list):
        return json.dumps(
            [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                for item in value
            ]
        )
    return str(value)


def _record_provider_failure(
    session: Session,
    document: Document,
    extractor: Extractor,
    provider: ExtractionProvider,
    exc: ProviderError,
) -> Extraction:
    """Store the failure against both the extraction and the document.

    A row is still written so the attempt — and what it cost in wall time and
    debugging value — is not lost. There is no response to keep, so
    `raw_response` records the error instead.
    """
    message = f"{type(exc).__name__}: {exc}"
    extraction = Extraction(
        document_id=document.id,
        # The call never returned, so the model name comes from whatever the
        # provider says it would have used.
        model_name=getattr(provider, "model_name", get_settings().extraction_model),
        prompt_version=extractor.prompt_version,
        raw_response={"error": message},
        error=message,
    )
    document.status = DocumentStatus.FAILED
    document.error = message
    session.add(extraction)
    session.commit()
    session.refresh(extraction)
    return extraction


def extract_document_in_background(document_id: uuid.UUID) -> None:
    """Entry point for `BackgroundTasks`; owns its own session."""
    with get_sessionmaker()() as session:
        document = session.get(Document, document_id)
        if document is None:
            logger.error("document %s vanished before extraction", document_id)
            return

        try:
            extract_document(session, document)
        except (ExtractionError, UnknownDocumentType) as exc:
            logger.warning("extraction not attempted for %s: %s", document_id, exc)
            session.rollback()
            document.status = DocumentStatus.FAILED
            document.error = str(exc)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - last line of defence
            logger.exception("unexpected extraction failure for %s", document_id)
            session.rollback()
            document.status = DocumentStatus.FAILED
            document.error = f"Unexpected extraction failure: {exc}"
            session.commit()
