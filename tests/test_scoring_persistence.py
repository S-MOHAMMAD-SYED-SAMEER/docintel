"""Scoring as it lands in the database, end to end through the service."""

import json
import uuid

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import extraction as extraction_service
from app.config import get_settings
from app.models import Document, DocumentStatus, Extraction

from .conftest import VALID_INVOICE_PAYLOAD, FakeProvider

# Every field present, every value on the page, every confidence high — the
# case that should sail through without review.
CONFIDENT_PAYLOAD = json.loads(json.dumps(VALID_INVOICE_PAYLOAD))
for _name, _field in CONFIDENT_PAYLOAD.items():
    _field["confidence"] = 0.99
CONFIDENT_PAYLOAD["purchase_order_number"] = {
    "value": "PO-88",
    "confidence": 0.99,
    "source_page": 1,
}


def _run(engine: Engine, document: Document, provider: FakeProvider) -> uuid.UUID:
    with Session(engine) as session:
        attached = session.get(Document, document.id)
        assert attached is not None
        return extraction_service.extract_document(session, attached, provider).id


def _fields(engine: Engine, extraction_id: uuid.UUID) -> dict:
    with Session(engine) as session:
        extraction = session.get(Extraction, extraction_id)
        assert extraction is not None
        return {value.field_name: value for value in extraction.field_values}


def _document(engine: Engine, document_id: uuid.UUID) -> Document:
    with Session(engine) as session:
        document = session.get(Document, document_id)
        assert document is not None
        session.expunge(document)
        return document


# --- what gets stored -----------------------------------------------------


def test_final_confidence_is_persisted(
    text_layer_document: Document, migrated_engine: Engine
) -> None:
    extraction_id = _run(migrated_engine, text_layer_document, FakeProvider())

    total = _fields(migrated_engine, extraction_id)["total"]
    # model 0.98 * 0.4 + arithmetic 0.2 + text layer 0.2, renormalised over 0.8.
    assert total.confidence == pytest.approx((0.4 * 0.98 + 0.2 + 0.2) / 0.8)
    assert total.confidence != total.model_confidence


def test_model_confidence_survives_scoring(
    text_layer_document: Document, migrated_engine: Engine
) -> None:
    """Milestone 4's information is not destroyed by milestone 5's."""
    extraction_id = _run(migrated_engine, text_layer_document, FakeProvider())

    fields = _fields(migrated_engine, extraction_id)
    for name, field in fields.items():
        assert field.model_confidence == pytest.approx(
            VALID_INVOICE_PAYLOAD[name]["confidence"]
        )


def test_the_parsed_json_still_holds_the_model_confidence(
    text_layer_document: Document, migrated_engine: Engine
) -> None:
    extraction_id = _run(migrated_engine, text_layer_document, FakeProvider())

    with Session(migrated_engine) as session:
        parsed = session.get(Extraction, extraction_id).parsed

    assert parsed["total"]["confidence"] == pytest.approx(0.98)


def test_the_scoring_breakdown_is_persisted(
    text_layer_document: Document, migrated_engine: Engine
) -> None:
    """A flagged field must be able to say why it was flagged."""
    extraction_id = _run(migrated_engine, text_layer_document, FakeProvider())

    validation = _fields(migrated_engine, extraction_id)["total"].validation
    names = {signal["name"] for signal in validation["signals"]}
    assert names == {"model", "arithmetic", "text_layer"}
    assert validation["blocking_failures"] == []


# --- needs_review ---------------------------------------------------------


def test_a_confident_corroborated_document_needs_no_review(
    text_layer_document: Document, migrated_engine: Engine
) -> None:
    extraction_id = _run(
        migrated_engine, text_layer_document, FakeProvider(payload=CONFIDENT_PAYLOAD)
    )

    fields = _fields(migrated_engine, extraction_id)
    flagged = {name for name, field in fields.items() if field.needs_review}
    assert flagged == set()
    assert _document(migrated_engine, text_layer_document.id).status is (
        DocumentStatus.EXTRACTED
    )


def test_an_arithmetic_failure_flags_its_fields_and_the_document(
    text_layer_document: Document, migrated_engine: Engine
) -> None:
    payload = json.loads(json.dumps(CONFIDENT_PAYLOAD))
    payload["total"]["value"] = "1500.00"

    extraction_id = _run(
        migrated_engine, text_layer_document, FakeProvider(payload=payload)
    )

    fields = _fields(migrated_engine, extraction_id)
    for name in ("subtotal", "tax", "total"):
        assert fields[name].needs_review is True, name
        assert fields[name].validation["blocking_failures"]
    # A field the failure does not bear on is left alone.
    assert fields["vendor_name"].needs_review is False
    assert _document(migrated_engine, text_layer_document.id).status is (
        DocumentStatus.NEEDS_REVIEW
    )


def test_a_bad_currency_code_flags_only_the_currency(
    text_layer_document: Document, migrated_engine: Engine
) -> None:
    payload = json.loads(json.dumps(CONFIDENT_PAYLOAD))
    payload["currency"]["value"] = "EURO"

    extraction_id = _run(
        migrated_engine, text_layer_document, FakeProvider(payload=payload)
    )

    fields = _fields(migrated_engine, extraction_id)
    assert fields["currency"].needs_review is True
    assert "currency.iso_4217" in fields["currency"].validation["blocking_failures"]
    assert fields["total"].needs_review is False


def test_the_threshold_decides_which_fields_are_flagged(
    text_layer_document: Document, migrated_engine: Engine, monkeypatch
) -> None:
    monkeypatch.setenv("DOCINTEL_CONFIDENCE_THRESHOLD", "0.999")
    get_settings.cache_clear()
    try:
        extraction_id = _run(
            migrated_engine,
            text_layer_document,
            FakeProvider(payload=CONFIDENT_PAYLOAD),
        )
        fields = _fields(migrated_engine, extraction_id)
    finally:
        get_settings.cache_clear()

    # Same data that passes at 0.85 is flagged at 0.999.
    assert all(field.needs_review for field in fields.values())
    assert _document(migrated_engine, text_layer_document.id).status is (
        DocumentStatus.NEEDS_REVIEW
    )


def test_the_weights_change_what_gets_stored(
    text_layer_document: Document, migrated_engine: Engine, monkeypatch
) -> None:
    baseline = _fields(
        migrated_engine, _run(migrated_engine, text_layer_document, FakeProvider())
    )["total"].confidence

    monkeypatch.setenv("DOCINTEL_CONFIDENCE_WEIGHT_MODEL", "0.05")
    get_settings.cache_clear()
    try:
        reweighted = _fields(
            migrated_engine,
            _run(migrated_engine, text_layer_document, FakeProvider()),
        )["total"].confidence
    finally:
        get_settings.cache_clear()

    assert reweighted != pytest.approx(baseline)


# --- the text layer, end to end -------------------------------------------


def test_a_value_absent_from_the_text_layer_scores_lower_than_one_present(
    text_layer_document: Document, migrated_engine: Engine
) -> None:
    on_the_page = _fields(
        migrated_engine,
        _run(migrated_engine, text_layer_document, FakeProvider(CONFIDENT_PAYLOAD)),
    )["invoice_number"].confidence

    invented = json.loads(json.dumps(CONFIDENT_PAYLOAD))
    invented["invoice_number"]["value"] = "INV-0000-9999"
    not_on_the_page = _fields(
        migrated_engine,
        _run(migrated_engine, text_layer_document, FakeProvider(invented)),
    )["invoice_number"].confidence

    assert not_on_the_page < on_the_page


def test_a_document_with_no_text_layer_is_not_penalised(
    rendered_document: Document, text_layer_document: Document, migrated_engine: Engine
) -> None:
    """The blank-page fixture has no text layer; its fields still score well."""
    scanned = _fields(
        migrated_engine,
        _run(migrated_engine, rendered_document, FakeProvider(CONFIDENT_PAYLOAD)),
    )["total"]

    assert scanned.needs_review is False
    assert {signal["name"] for signal in scanned.validation["signals"]} == {
        "model",
        "arithmetic",
    }
