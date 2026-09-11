"""GET /api/v1/review and POST /api/v1/review/{field_id}."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Document, DocumentStatus, FieldValue

from .conftest import VALID_INVOICE_PAYLOAD

# Every field present, confident, and corroborated by the fixture's text layer
# — nothing here should reach the queue.
CONFIDENT_PAYLOAD = json.loads(json.dumps(VALID_INVOICE_PAYLOAD))
for _field in CONFIDENT_PAYLOAD.values():
    _field["confidence"] = 0.99
CONFIDENT_PAYLOAD["purchase_order_number"] = {
    "value": "PO-88",
    "confidence": 0.99,
    "source_page": 1,
}


# Several fields the model was unsure about, so the queue is long enough to
# page through and to have a meaningful order.
MANY_FLAGGED_PAYLOAD = json.loads(json.dumps(CONFIDENT_PAYLOAD))
for _name, _confidence in (
    ("vendor_address", 0.10),
    ("vendor_tax_id", 0.20),
    ("customer_name", 0.30),
    ("due_date", 0.40),
    ("purchase_order_number", 0.50),
):
    MANY_FLAGGED_PAYLOAD[_name]["confidence"] = _confidence


def _payload_with(**overrides: object) -> dict:
    payload = json.loads(json.dumps(CONFIDENT_PAYLOAD))
    for field_name, value in overrides.items():
        payload[field_name]["value"] = value
    return payload


def _items(client: TestClient, **params: object) -> list[dict]:
    response = client.get("/api/v1/review", params=params)
    assert response.status_code == 200
    return response.json()["items"]


def _by_name(items: list[dict]) -> dict[str, dict]:
    return {item["field_name"]: item for item in items}


# --- what is in the queue -------------------------------------------------


def test_empty_queue_before_anything_is_extracted(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    body = api_client.get("/api/v1/review").json()

    assert body["total"] == 0
    assert body["items"] == []
    assert body["confidence_threshold"] == 0.85


def test_a_fully_confident_extraction_leaves_the_queue_empty(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    body = api_client.get("/api/v1/review").json()

    assert body["total"] == 0
    assert body["items"] == []


def test_queue_returns_only_fields_needing_review(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document()

    queued = {item["field_name"] for item in _items(api_client)}

    with Session(migrated_engine) as session:
        flagged = {
            value.field_name
            for value in session.query(FieldValue).all()
            if value.needs_review
        }
        unflagged = {
            value.field_name
            for value in session.query(FieldValue).all()
            if not value.needs_review
        }

    assert queued == flagged
    assert queued.isdisjoint(unflagged)
    assert unflagged  # the fixture really does have fields that pass


def test_every_queued_field_is_marked_needs_review(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document()

    assert all(item["needs_review"] for item in _items(api_client))


def test_a_low_confidence_field_is_queued(
    api_client: TestClient, extracted_document
) -> None:
    payload = _payload_with()
    payload["vendor_tax_id"]["confidence"] = 0.1

    extracted_document(payload)

    assert "vendor_tax_id" in _by_name(_items(api_client))


def test_a_field_failing_a_deterministic_check_is_queued(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    queued = _by_name(_items(api_client))
    for field_name in ("subtotal", "tax", "total"):
        assert field_name in queued, field_name
        assert "totals.subtotal_plus_tax_equals_total" in queued[field_name][
            "failed_checks"
        ]


def test_queue_is_ordered_least_confident_first(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    confidences = [item["confidence"] for item in _items(api_client)]

    assert len(confidences) >= 5
    assert confidences == sorted(confidences)


# --- what each entry carries ----------------------------------------------


def test_entry_carries_the_document_metadata(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    document = _by_name(_items(api_client))["total"]["document"]
    assert document["id"] == str(text_layer_document.id)
    assert document["filename"] == "acme-invoice.pdf"
    assert document["doc_type"] == "invoice"
    assert document["status"] == DocumentStatus.NEEDS_REVIEW
    assert document["page_count"] == 2


def test_entry_carries_the_extraction_provenance(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    item = _by_name(_items(api_client))["total"]
    assert item["model_name"] == "fake-model-1"
    assert item["prompt_version"] == "invoice/v1"
    uuid.UUID(item["extraction_id"])
    uuid.UUID(item["field_id"])


def test_entry_exposes_the_extracted_value(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    assert _by_name(_items(api_client))["total"]["value"] == "1500.00"


def test_confidence_is_the_final_score_from_the_database(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    """The queue reports what was persisted; it never rescores."""
    extracted_document(_payload_with(total="1500.00"))

    item = _by_name(_items(api_client))["total"]
    with Session(migrated_engine) as session:
        stored = (
            session.query(FieldValue).filter_by(field_name="total").one()
        )

    assert item["confidence"] == pytest.approx(stored.confidence)
    assert item["model_confidence"] == pytest.approx(stored.model_confidence)


def test_model_confidence_remains_available_and_distinct(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    item = _by_name(_items(api_client))["total"]
    assert item["model_confidence"] == pytest.approx(0.99)
    assert item["confidence"] < item["model_confidence"]


def test_source_page_is_exposed(
    api_client: TestClient, extracted_document
) -> None:
    payload = _payload_with()
    payload["vendor_address"]["confidence"] = 0.1

    extracted_document(payload)

    assert _by_name(_items(api_client))["vendor_address"]["source_page"] == 1


def test_a_null_source_page_is_exposed_as_null(
    api_client: TestClient, extracted_document
) -> None:
    payload = _payload_with()
    payload["purchase_order_number"] = {
        "value": None,
        "confidence": 0.2,
        "source_page": None,
    }

    extracted_document(payload)

    item = _by_name(_items(api_client))["purchase_order_number"]
    assert item["source_page"] is None
    assert item["value"] is None


def test_signal_breakdown_is_exposed(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    signals = _by_name(_items(api_client))["total"]["signals"]
    names = {signal["name"] for signal in signals}
    assert {"model", "arithmetic"} <= names
    assert all({"name", "weight", "score"} <= set(signal) for signal in signals)


def test_review_reason_names_the_failed_check(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    reason = _by_name(_items(api_client))["total"]["review_reason"]
    assert "totals.subtotal_plus_tax_equals_total" in reason


def test_review_reason_for_plain_low_confidence_mentions_only_the_threshold(
    api_client: TestClient, extracted_document
) -> None:
    payload = _payload_with()
    payload["customer_name"]["confidence"] = 0.1

    extracted_document(payload)

    item = _by_name(_items(api_client))["customer_name"]
    assert item["failed_checks"] == []
    assert "below the review threshold" in item["review_reason"]


def test_a_missing_text_layer_is_not_reported_as_a_miss(
    api_client: TestClient, rendered_document: Document, migrated_engine: Engine
) -> None:
    """The blank-page fixture has no text layer; milestone 5 omits the signal."""
    from app import extraction as extraction_service

    from .conftest import FakeProvider

    payload = _payload_with()
    payload["customer_name"]["confidence"] = 0.1
    with Session(migrated_engine) as session:
        document = session.get(Document, rendered_document.id)
        extraction_service.extract_document(
            session, document, FakeProvider(payload=payload)
        )

    item = _by_name(_items(api_client))["customer_name"]
    assert item["text_layer_miss"] is False
    assert "text layer" not in item["review_reason"]
    assert all(signal["name"] != "text_layer" for signal in item["signals"])


def test_a_real_text_layer_miss_is_reported(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(invoice_number="INV-0000-9999"))

    item = _by_name(_items(api_client))["invoice_number"]
    assert item["text_layer_miss"] is True
    assert "not found in the page text layer" in item["review_reason"]


# --- paging ---------------------------------------------------------------


def test_total_counts_the_whole_queue_not_the_page(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    body = api_client.get("/api/v1/review", params={"limit": 2}).json()

    assert len(body["items"]) == 2
    assert body["total"] > 2
    assert body["limit"] == 2
    assert body["offset"] == 0


def test_offset_pages_through_the_queue(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    first = _items(api_client, limit=2, offset=0)
    second = _items(api_client, limit=2, offset=2)

    assert {item["field_id"] for item in first}.isdisjoint(
        item["field_id"] for item in second
    )


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (201, 0), (10, -1)])
def test_invalid_paging_is_rejected(
    api_client: TestClient, limit: int, offset: int
) -> None:
    response = api_client.get(
        "/api/v1/review", params={"limit": limit, "offset": offset}
    )

    assert response.status_code == 422


# --- the threshold the rows were judged against ---------------------------


def test_threshold_is_echoed_from_settings(
    api_client: TestClient, migrated_engine: Engine, monkeypatch
) -> None:
    monkeypatch.setenv("DOCINTEL_CONFIDENCE_THRESHOLD", "0.6")
    get_settings.cache_clear()
    try:
        body = api_client.get("/api/v1/review").json()
    finally:
        get_settings.cache_clear()

    assert body["confidence_threshold"] == 0.6


def test_the_queue_does_not_rescore_when_the_threshold_changes(
    api_client: TestClient, extracted_document, monkeypatch
) -> None:
    """needs_review is a stored decision, not one the queue makes."""
    extracted_document()
    before = {item["field_name"] for item in _items(api_client)}

    monkeypatch.setenv("DOCINTEL_CONFIDENCE_THRESHOLD", "0.01")
    get_settings.cache_clear()
    try:
        after = {item["field_name"] for item in _items(api_client)}
    finally:
        get_settings.cache_clear()

    assert after == before


# --- POST /api/v1/review/{field_id} ---------------------------------------


def test_correcting_a_field_takes_it_out_of_the_queue(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field_id = _by_name(_items(api_client))["total"]["field_id"]

    response = api_client.post(
        f"/api/v1/review/{field_id}", json={"corrected_value": "1210.00"}
    )

    assert response.status_code == 201
    assert "total" not in _by_name(_items(api_client))
    with Session(migrated_engine) as session:
        stored = session.get(FieldValue, uuid.UUID(field_id))
        assert stored.value == "1210.00"
        assert stored.needs_review is False


def test_correction_endpoint_404s_for_an_unknown_field(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    response = api_client.post(
        f"/api/v1/review/{uuid.uuid4()}", json={"corrected_value": "x"}
    )

    assert response.status_code == 404


def test_correction_endpoint_rejects_a_malformed_field_id(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    response = api_client.post(
        "/api/v1/review/not-a-uuid", json={"corrected_value": "x"}
    )

    assert response.status_code == 422


# --- document status ------------------------------------------------------


def test_reading_the_queue_does_not_change_document_status(
    api_client: TestClient, extracted_document, text_layer_document: Document,
    migrated_engine: Engine
) -> None:
    extracted_document()
    api_client.get("/api/v1/review")

    with Session(migrated_engine) as session:
        document = session.get(Document, text_layer_document.id)
        assert document.status is DocumentStatus.NEEDS_REVIEW


def test_a_clean_document_stays_extracted(
    api_client: TestClient, extracted_document, text_layer_document: Document,
    migrated_engine: Engine
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)
    api_client.get("/api/v1/review")

    with Session(migrated_engine) as session:
        document = session.get(Document, text_layer_document.id)
        assert document.status is DocumentStatus.EXTRACTED
