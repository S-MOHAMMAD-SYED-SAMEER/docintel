"""Exporting the current record of a document.

"Current" means after review: a corrected field exports the human's value, an
uncorrected one exports the model's. Raw model responses are not part of the
export — they stay in `extractions.raw_response` for debugging and evals, and
a user-facing record has no use for them.
"""

import csv
import io
import json
import uuid
from typing import Any, get_args, get_origin

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.extractors import UnknownDocumentType, get_extractor
from app.models import Document, Extraction, FieldValue

JSON = "json"
CSV = "csv"
SUPPORTED_FORMATS = (JSON, CSV)

CSV_COLUMNS = (
    "document_id",
    "filename",
    "doc_type",
    "document_status",
    "field_name",
    "value",
    "corrected",
    "original_value",
    "confidence",
    "model_confidence",
    "source_page",
    "needs_review",
)


class ExportError(Exception):
    """The document cannot be exported."""


def structured_field_names(schema: type[BaseModel]) -> frozenset[str]:
    """Schema fields whose value is a list, so JSON can nest them properly.

    Read off the extractor's schema rather than guessed from the stored text —
    `field_values.value` is text for every field, and sniffing for a leading
    bracket would misread a value that merely looks like JSON.
    """
    names = set()
    for name, field in schema.model_fields.items():
        annotation = getattr(field.annotation, "model_fields", {}).get("value")
        if annotation is None:
            continue
        if any(
            get_origin(argument) in (list, tuple)
            for argument in get_args(annotation.annotation)
        ):
            names.add(name)
    return frozenset(names)


def latest_extraction(session: Session, document: Document) -> Extraction | None:
    """The most recent extraction for a document, with its fields loaded."""
    statement = (
        select(Extraction)
        .where(Extraction.document_id == document.id)
        .where(Extraction.parsed.is_not(None))
        .order_by(Extraction.created_at.desc(), Extraction.id.desc())
        .limit(1)
        .options(selectinload(Extraction.field_values).selectinload(
            FieldValue.corrections
        ))
    )
    return session.execute(statement).scalars().first()


def get_document(session: Session, document_id: uuid.UUID) -> Document | None:
    return session.get(Document, document_id)


def _field_entry(
    field_value: FieldValue, structured: frozenset[str]
) -> dict[str, Any]:
    value: Any = field_value.value
    if field_value.field_name in structured and value is not None:
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            # A reviewer typed free text into a structured field. Export what
            # they wrote rather than dropping it.
            pass

    entry: dict[str, Any] = {
        "value": value,
        "corrected": field_value.is_corrected,
        "source_page": field_value.source_page,
        "needs_review": field_value.needs_review,
        # These describe the model's answer, not the corrected one.
        "confidence": field_value.confidence,
        "model_confidence": field_value.model_confidence,
    }
    if field_value.is_corrected:
        entry["original_value"] = field_value.corrections[0].original_value
        entry["corrected_at"] = field_value.corrections[-1].corrected_at.isoformat()
    return entry


def build_record(session: Session, document: Document) -> dict[str, Any]:
    """The document's current record, as a plain JSON-safe dict."""
    extraction = latest_extraction(session, document)
    if extraction is None:
        raise ExportError(
            f"Document {document.id} has no completed extraction to export."
        )

    try:
        structured = structured_field_names(get_extractor(document.doc_type).schema)
    except UnknownDocumentType:
        # Export what is stored even if the extractor has since been removed.
        structured = frozenset()

    return {
        "document": {
            "id": str(document.id),
            "filename": document.filename,
            "doc_type": document.doc_type,
            "status": str(document.status),
            "page_count": document.page_count,
            "uploaded_at": document.uploaded_at.isoformat(),
        },
        "extraction": {
            "id": str(extraction.id),
            "model_name": extraction.model_name,
            "prompt_version": extraction.prompt_version,
            "extracted_at": extraction.created_at.isoformat(),
        },
        "fields": {
            field_value.field_name: _field_entry(field_value, structured)
            for field_value in sorted(
                extraction.field_values, key=lambda value: value.field_name
            )
        },
    }


def to_csv(record: dict[str, Any]) -> str:
    """One row per field: flat, spreadsheet-friendly, no Python reprs.

    A structured value (the line-item table) is written as compact JSON in its
    cell — a standard, parseable representation rather than `repr()` output.
    """
    document = record["document"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()

    for field_name, entry in record["fields"].items():
        value = entry["value"]
        writer.writerow(
            {
                "document_id": document["id"],
                "filename": document["filename"],
                "doc_type": document["doc_type"],
                "document_status": document["status"],
                "field_name": field_name,
                "value": _cell(value),
                "corrected": str(entry["corrected"]).lower(),
                "original_value": _cell(entry.get("original_value")),
                "confidence": f"{entry['confidence']:.4f}",
                "model_confidence": f"{entry['model_confidence']:.4f}",
                "source_page": (
                    "" if entry["source_page"] is None else entry["source_page"]
                ),
                "needs_review": str(entry["needs_review"]).lower(),
            }
        )

    return buffer.getvalue()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))
