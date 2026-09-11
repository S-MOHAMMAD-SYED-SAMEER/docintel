"""The server-rendered review page at GET /review."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.models import Document, DocumentStatus

from .conftest import FakeProvider, VALID_INVOICE_PAYLOAD
from .test_review_api import CONFIDENT_PAYLOAD, MANY_FLAGGED_PAYLOAD, _payload_with


def _html(client: TestClient, **params: object) -> str:
    response = client.get("/review", params=params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


# --- rendering ------------------------------------------------------------


def test_page_renders_with_an_empty_queue(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    html = _html(api_client)

    assert "Review queue" in html
    assert "Nothing to review." in html


def test_page_renders_with_items(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    html = _html(api_client)

    assert "Nothing to review." not in html
    assert "<table>" in html


def test_page_reports_the_queue_size_and_threshold(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)
    total = api_client.get("/api/v1/review").json()["total"]

    html = _html(api_client)

    assert f"{total} fields awaiting review" in html
    assert "threshold of 0.85" in html


def test_a_confident_extraction_leaves_the_page_empty(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    assert "Nothing to review." in _html(api_client)


# --- what the reviewer can see --------------------------------------------


def test_document_information_is_visible(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    html = _html(api_client)

    assert "acme-invoice.pdf" in html
    assert "invoice" in html
    assert "status needs_review" in html
    assert "2 page(s)" in html
    assert "fake-model-1" in html
    assert "invoice/v1" in html


def test_field_names_and_values_are_visible(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    html = _html(api_client)

    assert "total" in html
    assert "1500.00" in html


def test_confidence_information_is_visible(
    api_client: TestClient, extracted_document
) -> None:
    """Both numbers: the final score and what the model claimed."""
    extracted_document(_payload_with(total="1500.00"))
    item = next(
        item
        for item in api_client.get("/api/v1/review").json()["items"]
        if item["field_name"] == "total"
    )

    html = _html(api_client)

    assert f"{item['confidence']:.3f}" in html
    assert "Model said" in html
    assert f"{item['model_confidence']:.2f}" in html


def test_source_page_is_visible(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    html = _html(api_client)

    assert "<th>Page</th>" in html


def test_validation_failures_are_visible(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    html = _html(api_client)

    assert "totals.subtotal_plus_tax_equals_total" in html
    assert 'class="fail">failed' in html


def test_a_field_with_no_failed_checks_says_so(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    html = _html(api_client)

    assert "no failed checks" in html


def test_the_signal_breakdown_is_visible(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    html = _html(api_client)

    assert "model:" in html
    assert "weight 0.40" in html


def test_the_review_reason_is_visible(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    html = _html(api_client)

    assert "Why it needs review" in html
    assert "deterministic checks failed" in html


def test_a_missing_value_is_shown_as_not_found(
    api_client: TestClient, extracted_document
) -> None:
    payload = json.loads(json.dumps(CONFIDENT_PAYLOAD))
    payload["purchase_order_number"] = {
        "value": None,
        "confidence": 0.2,
        "source_page": None,
    }

    extracted_document(payload)

    assert "not found" in _html(api_client)


# --- boundaries -----------------------------------------------------------


def test_the_page_shows_no_correction_form(
    api_client: TestClient, extracted_document
) -> None:
    """Corrections are milestone 7; this page is read-only."""
    extracted_document(MANY_FLAGGED_PAYLOAD)

    html = _html(api_client)

    assert "<form" not in html
    assert "<input" not in html


def test_the_page_introduces_no_frontend_framework(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    html = _html(api_client).lower()

    assert "<script" not in html
    assert "react" not in html


def test_the_page_is_not_in_the_openapi_schema(api_client: TestClient) -> None:
    """It is a page, not part of the JSON API contract."""
    paths = api_client.get("/openapi.json").json()["paths"]

    assert "/review" not in paths
    assert "/api/v1/review" in paths


def test_paging_applies_to_the_page_too(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    html = _html(api_client, limit=1)

    assert "Showing 1." in html


def test_a_document_with_no_text_layer_shows_no_text_layer_signal(
    api_client: TestClient, rendered_document: Document, migrated_engine: Engine
) -> None:
    """Milestone 5 omitted the signal; the page must not imply a miss."""
    payload = json.loads(json.dumps(VALID_INVOICE_PAYLOAD))
    with Session(migrated_engine) as session:
        from app import extraction as extraction_service

        document = session.get(Document, rendered_document.id)
        extraction_service.extract_document(
            session, document, FakeProvider(payload=payload)
        )

    html = _html(api_client)

    assert "text_layer" not in html
    assert "not found in the page text layer" not in html


def test_reading_the_page_does_not_change_document_status(
    api_client: TestClient, extracted_document, text_layer_document: Document,
    migrated_engine: Engine
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)
    _html(api_client)

    with Session(migrated_engine) as session:
        document = session.get(Document, text_layer_document.id)
        assert document.status is DocumentStatus.NEEDS_REVIEW
