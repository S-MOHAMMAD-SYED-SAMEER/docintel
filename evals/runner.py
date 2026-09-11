"""Running a dataset through the real pipeline.

Every document goes through the same path a user's upload does — store,
render, extract, validate, score — so what is measured is the system, not a
shortcut through it. The only thing injected is the provider, so the harness
can be exercised without spending money.
"""

import json
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app import extraction as extraction_service
from app import ingestion
from app.models import Document, Extraction, FieldValue
from app.providers import ExtractionProvider
from evals.dataset import Dataset, LabelledDocument
from evals.metrics import DocumentObservation, FieldObservation, Report, summarise

logger = logging.getLogger(__name__)

DOC_TYPE = "invoice"


def evaluate(
    session: Session,
    dataset: Dataset,
    provider: ExtractionProvider,
    *,
    doc_type: str = DOC_TYPE,
) -> Report:
    """Run every document and fold the results into one report."""
    observations = [
        evaluate_document(session, labelled, provider, doc_type=doc_type)
        for labelled in dataset.documents
    ]
    return summarise(dataset.name, observations)


def evaluate_document(
    session: Session,
    labelled: LabelledDocument,
    provider: ExtractionProvider,
    *,
    doc_type: str = DOC_TYPE,
) -> DocumentObservation:
    """Push one labelled document through the pipeline and compare.

    A document that cannot be extracted is reported as failed and its labelled
    fields are counted as wrong — never quietly dropped, which would flatter
    every rate in the report.
    """
    try:
        document = ingestion.store_upload(
            session,
            filename=labelled.filename,
            doc_type=doc_type,
            data=labelled.path.read_bytes(),
        )
        ingestion.render_pages(session, document)
        extraction = extraction_service.extract_document(session, document, provider)
    except Exception as exc:  # noqa: BLE001 - a failure is a result, not a crash
        logger.warning("evaluation failed for %s: %s", labelled.document_id, exc)
        return _failed(labelled, f"{type(exc).__name__}: {exc}")

    if extraction.parsed is None:
        return _failed(
            labelled,
            extraction.error or "extraction produced no parsed record",
            extraction=extraction,
        )

    return DocumentObservation(
        document_id=labelled.document_id,
        fields=tuple(_observe(labelled, document, extraction)),
        cost_usd=extraction.cost_usd,
        latency_ms=extraction.latency_ms,
        model_name=extraction.model_name,
        prompt_version=extraction.prompt_version,
    )


def _observe(
    labelled: LabelledDocument, document: Document, extraction: Extraction
) -> list[FieldObservation]:
    """One observation per *labelled* field.

    Driven by the labels, not by what the model returned: a field the model
    omitted entirely still has to be counted, as wrong.
    """
    by_name: dict[str, FieldValue] = {
        value.field_name: value for value in extraction.field_values
    }

    observations = []
    for field_name, expected in labelled.fields.items():
        field_value = by_name.get(field_name)
        observations.append(
            FieldObservation(
                document_id=labelled.document_id,
                field_name=field_name,
                expected=expected,
                extracted=_extracted_value(field_name, field_value),
                # A field the model never produced has nobody looking at it.
                needs_review=bool(field_value and field_value.needs_review),
                confidence=field_value.confidence if field_value else 0.0,
                model_confidence=field_value.model_confidence if field_value else 0.0,
            )
        )
    return observations


def _extracted_value(field_name: str, field_value: FieldValue | None) -> Any:
    if field_value is None or field_value.value is None:
        return None
    if field_name == "line_items":
        try:
            return json.loads(field_value.value)
        except json.JSONDecodeError:
            return field_value.value
    return field_value.value


def _failed(
    labelled: LabelledDocument, error: str, extraction: Extraction | None = None
) -> DocumentObservation:
    fields = tuple(
        FieldObservation(
            document_id=labelled.document_id,
            field_name=field_name,
            expected=expected,
            extracted=None,
            needs_review=False,
            confidence=0.0,
            model_confidence=0.0,
            extraction_failed=True,
        )
        for field_name, expected in labelled.fields.items()
    )
    return DocumentObservation(
        document_id=labelled.document_id,
        fields=fields,
        cost_usd=extraction.cost_usd if extraction else None,
        latency_ms=extraction.latency_ms if extraction else None,
        model_name=extraction.model_name if extraction else None,
        prompt_version=extraction.prompt_version if extraction else None,
        error=error,
    )


def persist(session: Session, report: Report, *, model_name: str, prompt_version: str):
    """Record the run so a prompt change always has a before/after number."""
    from app.models import EvalRun

    mean_cost = report.mean_cost_usd
    run = EvalRun(
        dataset_name=report.dataset_name,
        prompt_version=prompt_version,
        model_name=model_name,
        document_count=report.documents,
        field_accuracy={
            name: accuracy.as_dict()
            for name, accuracy in report.field_accuracy.items()
        },
        review_rate=report.review_rate,
        false_confident_rate=report.false_confident_rate,
        mean_cost_usd=Decimal(mean_cost) if mean_cost is not None else None,
        mean_latency_ms=report.mean_latency_ms,
        metrics=report.as_dict(),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run
