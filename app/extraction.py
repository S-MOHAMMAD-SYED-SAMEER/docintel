"""The extractor leg of the pipeline: rendered pages in, stored fields out.

Sits beside `app.ingestion` and follows the same shape — the route handler does
HTTP, this module owns the order of operations and the status transitions.

    PROCESSING    pages rendered, extraction running
    EXTRACTED     the answer parsed and every field cleared the threshold
    NEEDS_REVIEW  at least one field is uncertain or failed a check
    FAILED        the provider or the parse gave up; `error` says why

The order of operations, once the model has answered:

    raw extraction -> schema validation -> deterministic validation
        -> page-text checks -> final field confidence -> needs_review
        -> persistence
"""

import json
import logging
import mimetypes
import uuid
from collections.abc import Iterator, Mapping

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app import media as media_types
from app import rendering, storage
from app.confidence import FieldScore, score_extraction
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.extractors import UnknownDocumentType, get_extractor
from app.extractors.base import ExtractedField, Extractor
from app.media import UnsupportedMediaType
from app.models import Document, DocumentStatus, Extraction, FieldValue
from app.providers import ExtractionProvider, PageImage, ProviderError, get_provider
from app.validation import get_validator
from app.validation.text_layer import TextLayer

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


def load_text_layer(document: Document) -> TextLayer:
    """The document's own embedded text, if it has any.

    A scan, a photo or an image upload has no text layer. That is recorded as
    an empty `TextLayer`, which scoring treats as "this signal does not apply"
    — never as evidence that a field is wrong.
    """
    source = storage.resolve(document.storage_path)
    if not source.is_file():
        logger.warning("source file missing for document %s", document.id)
        return TextLayer()

    try:
        with source.open("rb") as handle:
            media = media_types.detect(handle.read(media_types.SNIFF_BYTES))
    except (UnsupportedMediaType, OSError) as exc:
        logger.warning("cannot read source of document %s: %s", document.id, exc)
        return TextLayer()

    return TextLayer.from_pages(rendering.extract_text_layer(source, media))


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

    # Deterministic checks, then the text layer, then the score. Anything the
    # maths can settle is settled in Python rather than taken on trust.
    report = get_validator(document.doc_type)(parsed)
    text_layer = load_text_layer(document)
    value_texts = {
        field_name: _as_text(getattr(parsed, field_name).value)
        for field_name in type(parsed).model_fields
    }
    scores = score_extraction(parsed, report, text_layer, value_texts)

    extraction.parsed = parsed.model_dump(mode="json")
    extraction.field_values = list(build_field_values(parsed, scores))

    flagged = [score for score in scores.values() if score.needs_review]
    document.status = (
        DocumentStatus.NEEDS_REVIEW if flagged else DocumentStatus.EXTRACTED
    )
    document.error = None
    session.add(extraction)
    session.commit()
    session.refresh(extraction)

    logger.info(
        "extracted document %s with %s (%s): %d field(s), %d needing review, "
        "%d check(s) failed, text layer %s, %s USD",
        document.id,
        raw.model_name,
        extractor.prompt_version,
        len(extraction.field_values),
        len(flagged),
        len(report.failures()),
        "present" if text_layer.exists else "absent",
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


def build_field_values(
    parsed: BaseModel, scores: Mapping[str, FieldScore]
) -> Iterator[FieldValue]:
    """One row per top-level field of the schema.

    `confidence` holds the final combined score and `model_confidence` holds
    what the model said about itself — the second is never overwritten by the
    first, because the gap between them is how a confidently wrong model gets
    caught. A structured value (the line-item table) is stored as JSON text;
    `field_values.value` is text because a reviewer corrects what the document
    says.
    """
    for field_name in type(parsed).model_fields:
        field = getattr(parsed, field_name)
        if not isinstance(field, ExtractedField):
            raise ExtractionError(
                f"Schema field {field_name!r} is not an ExtractedField; "
                "extraction schemas must report confidence per field."
            )

        score = scores[field_name]
        yield FieldValue(
            field_name=field_name,
            value=_as_text(field.value),
            confidence=score.confidence,
            model_confidence=score.model_confidence,
            needs_review=score.needs_review,
            validation=score.as_dict(),
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


def extract_document_in_background(
    document_id: uuid.UUID,
    provider: ExtractionProvider | None = None,
) -> None:
    """Entry point for `BackgroundTasks`; owns its own session.

    `provider` is optional and defaults to `None`, exactly like
    `extract_document`'s own parameter, which this passes straight through.
    A caller that does not supply one keeps resolving the real, configured
    provider through `get_provider()` inside `extract_document` -- nothing
    here changes that default.
    """
    with get_sessionmaker()() as session:
        document = session.get(Document, document_id)
        if document is None:
            logger.error("document %s vanished before extraction", document_id)
            return

        try:
            extract_document(session, document, provider=provider)
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
