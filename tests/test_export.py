"""GET /api/v1/documents/{id}/export?format=json|csv."""

import csv
import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.models import Document, DocumentStatus

from .test_review_api import CONFIDENT_PAYLOAD, _payload_with


def _export(client: TestClient, document_id: uuid.UUID, **params: object):
    return client.get(f"/api/v1/documents/{document_id}/export", params=params)


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _correct(client: TestClient, field_name: str, value: str | None) -> None:
    item = next(
        item
        for item in client.get("/api/v1/review").json()["items"]
        if item["field_name"] == field_name
    )
    response = client.post(
        f"/api/v1/review/{item['field_id']}", json={"corrected_value": value}
    )
    assert response.status_code == 201


# --- JSON -----------------------------------------------------------------


def test_json_export_returns_the_record(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    response = _export(api_client, text_layer_document.id)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"document", "extraction", "fields"}


def test_json_export_carries_the_document_and_extraction(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    body = _export(api_client, text_layer_document.id).json()

    assert body["document"]["id"] == str(text_layer_document.id)
    assert body["document"]["filename"] == "acme-invoice.pdf"
    assert body["document"]["doc_type"] == "invoice"
    assert body["document"]["status"] == DocumentStatus.EXTRACTED
    assert body["extraction"]["model_name"] == "fake-model-1"
    assert body["extraction"]["prompt_version"] == "invoice/v1"


def test_json_export_includes_every_invoice_field(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    from app.extractors import Invoice

    extracted_document(CONFIDENT_PAYLOAD)

    body = _export(api_client, text_layer_document.id).json()

    assert set(body["fields"]) == set(Invoice.model_fields)


def test_uncorrected_values_are_exported(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    fields = _export(api_client, text_layer_document.id).json()["fields"]

    assert fields["invoice_number"]["value"] == "INV-2026-0042"
    assert fields["invoice_number"]["corrected"] is False
    assert "original_value" not in fields["invoice_number"]


def test_corrected_values_are_exported_with_their_original(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    _correct(api_client, "total", "1210.00")

    field = _export(api_client, text_layer_document.id).json()["fields"]["total"]

    assert field["value"] == "1210.00"
    assert field["corrected"] is True
    assert field["original_value"] == "1500.00"
    assert field["corrected_at"]


def test_the_export_keeps_the_model_confidence_against_the_model_answer(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    before = _export(api_client, text_layer_document.id).json()["fields"]["total"]
    _correct(api_client, "total", "1210.00")

    after = _export(api_client, text_layer_document.id).json()["fields"]["total"]

    assert after["model_confidence"] == before["model_confidence"]
    assert after["confidence"] == before["confidence"]


def test_line_items_are_a_nested_list_not_a_string(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    rows = _export(api_client, text_layer_document.id).json()["fields"]["line_items"]

    assert isinstance(rows["value"], list)
    assert len(rows["value"]) == 2
    assert rows["value"][0]["description"] == "Widget, blue"
    assert rows["value"][0]["amount"] == "400.00"


def test_a_null_field_exports_as_null(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document()

    field = _export(api_client, text_layer_document.id).json()["fields"][
        "purchase_order_number"
    ]

    assert field["value"] is None


def test_the_export_does_not_leak_the_raw_model_response(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    text = _export(api_client, text_layer_document.id).text

    assert "msg_01FakeExtraction" not in text
    assert "raw_response" not in text
    assert "input_tokens" not in text


def test_the_export_reflects_the_reviewed_status(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document()
    while queue := api_client.get("/api/v1/review").json()["items"]:
        api_client.post(
            f"/api/v1/review/{queue[0]['field_id']}",
            json={"corrected_value": "corrected"},
        )

    body = _export(api_client, text_layer_document.id).json()

    assert body["document"]["status"] == DocumentStatus.REVIEWED


# --- CSV ------------------------------------------------------------------


def test_csv_export_returns_csv(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    response = _export(api_client, text_layer_document.id, format="csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert ".csv" in response.headers["content-disposition"]


def test_csv_has_one_row_per_field(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    from app.extractors import Invoice

    extracted_document(CONFIDENT_PAYLOAD)

    rows = _rows(_export(api_client, text_layer_document.id, format="csv").text)

    assert {row["field_name"] for row in rows} == set(Invoice.model_fields)


def test_csv_carries_values_and_correction_state(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    _correct(api_client, "total", "1210.00")

    rows = {
        row["field_name"]: row
        for row in _rows(_export(api_client, text_layer_document.id, format="csv").text)
    }

    assert rows["total"]["value"] == "1210.00"
    assert rows["total"]["corrected"] == "true"
    assert rows["total"]["original_value"] == "1500.00"
    assert rows["invoice_number"]["corrected"] == "false"
    assert rows["invoice_number"]["original_value"] == ""


def test_csv_line_items_are_parseable_json_not_a_python_repr(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    rows = {
        row["field_name"]: row
        for row in _rows(_export(api_client, text_layer_document.id, format="csv").text)
    }

    cell = rows["line_items"]["value"]
    assert "InvoiceLineItem(" not in cell
    parsed = json.loads(cell)
    assert [row["description"] for row in parsed] == ["Widget, blue", "Widget, red"]


def test_csv_null_values_are_empty_cells(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document()

    rows = {
        row["field_name"]: row
        for row in _rows(_export(api_client, text_layer_document.id, format="csv").text)
    }

    assert rows["purchase_order_number"]["value"] == ""
    assert rows["purchase_order_number"]["source_page"] == ""


def test_csv_and_json_agree_on_the_values(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    _correct(api_client, "total", "1210.00")

    body = _export(api_client, text_layer_document.id).json()
    rows = {
        row["field_name"]: row
        for row in _rows(_export(api_client, text_layer_document.id, format="csv").text)
    }

    for name, entry in body["fields"].items():
        if isinstance(entry["value"], str) or entry["value"] is None:
            assert rows[name]["value"] == (entry["value"] or "")


# --- errors ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["xml", "pdf", "JSONL", ""])
def test_an_unsupported_format_is_rejected(
    api_client: TestClient, extracted_document, text_layer_document: Document, bad: str
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    response = _export(api_client, text_layer_document.id, format=bad)

    assert response.status_code == 422
    assert "Unsupported export format" in response.json()["detail"]


def test_format_is_case_insensitive(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    assert _export(api_client, text_layer_document.id, format="CSV").status_code == 200
    assert _export(api_client, text_layer_document.id, format="Json").status_code == 200


def test_json_is_the_default_format(
    api_client: TestClient, extracted_document, text_layer_document: Document
) -> None:
    extracted_document(CONFIDENT_PAYLOAD)

    response = _export(api_client, text_layer_document.id)

    assert response.headers["content-type"].startswith("application/json")


def test_an_unknown_document_is_a_404(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    response = _export(api_client, uuid.uuid4())

    assert response.status_code == 404


def test_a_document_with_no_extraction_is_a_409(
    api_client: TestClient, text_layer_document: Document, migrated_engine: Engine
) -> None:
    """Rendered but never extracted — there is no record to export yet."""
    response = _export(api_client, text_layer_document.id)

    assert response.status_code == 409
    assert "no completed extraction" in response.json()["detail"]
