"""The purchase-order document type, end to end through the shared pipeline.

Every assertion here is about reuse: the same extraction service, the same
confidence scoring, the same review queue, corrections and export that
invoices go through, selected by `doc_type` alone.
"""

import json
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app import extraction as extraction_service
from app.extractors import (
    Invoice,
    PurchaseOrder,
    PurchaseOrderExtractor,
    PurchaseOrderLineItem,
    get_extractor,
    registered_doc_types,
)
from app.extractors.purchase_order import PROMPT_VERSION
from app.models import Correction, Document, DocumentStatus, Extraction, FieldValue
from app.providers import ProviderError, RawExtraction
from app.validation import CheckStatus, get_validator
from app.validation.purchase_order import validate_purchase_order
from evals import dataset as dataset_module

DOC_TYPE = "purchase_order"

VALID_PO_PAYLOAD: dict[str, object] = {
    "purchase_order_number": {
        "value": "PO-2026-2001",
        "confidence": 0.97,
        "source_page": 1,
    },
    "order_date": {"value": "2026-02-03", "confidence": 0.95, "source_page": 1},
    "delivery_date": {"value": "2026-02-17", "confidence": 0.9, "source_page": 1},
    "vendor_name": {"value": "Acme Supplies BV", "confidence": 0.96, "source_page": 1},
    "customer_name": {"value": "Beta Ltd", "confidence": 0.94, "source_page": 1},
    "currency": {"value": "EUR", "confidence": 0.99, "source_page": 2},
    "subtotal": {"value": "600.00", "confidence": 0.93, "source_page": 2},
    "tax": {"value": "126.00", "confidence": 0.92, "source_page": 2},
    "total": {"value": "726.00", "confidence": 0.98, "source_page": 2},
    "line_items": {
        "value": [
            {
                "description": "Widget, blue",
                "quantity": "10",
                "unit_price": "40.00",
                "amount": "400.00",
            },
            {
                "description": "Widget, red",
                "quantity": "5",
                "unit_price": "40.00",
                "amount": "200.00",
            },
        ],
        "confidence": 0.88,
        "source_page": 2,
    },
}


def _payload(**overrides: object) -> dict:
    payload = json.loads(json.dumps(VALID_PO_PAYLOAD))
    for field_name, value in overrides.items():
        payload[field_name]["value"] = value
    return payload


def _confident(**overrides: object) -> dict:
    payload = _payload(**overrides)
    for field in payload.values():
        field["confidence"] = 0.99
    return payload


def _parsed(**overrides: object) -> PurchaseOrder:
    return PurchaseOrder.model_validate(_payload(**overrides))


def _status(parsed: PurchaseOrder, check_name: str) -> CheckStatus:
    report = validate_purchase_order(parsed)
    return next(
        check for check in report.checks if check.name == check_name
    ).status


class RecordingProvider:
    """A fake provider that records what the pipeline handed it."""

    model_name = "po-fake-1"

    def __init__(self, payload: dict | None = None, raises: Exception | None = None):
        self._payload = payload if payload is not None else VALID_PO_PAYLOAD
        self._raises = raises
        self.calls: list[dict] = []

    def extract(self, images, schema, prompt):
        self.calls.append(
            {"images": list(images), "schema": schema, "prompt": prompt}
        )
        if self._raises is not None:
            raise self._raises

        content = json.dumps(self._payload)
        return RawExtraction(
            content=content,
            raw_response={
                "id": "msg_po_fake",
                "model": self.model_name,
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 900, "output_tokens": 180},
            },
            model_name=self.model_name,
            input_tokens=900,
            output_tokens=180,
            cost_usd=Decimal("0.003600"),
            latency_ms=1400,
        )


@pytest.fixture
def po_document(api_client: TestClient, migrated_engine: Engine) -> Document:
    """An uploaded, rendered purchase order with a real text layer."""
    order = dataset_module.load("purchase_orders_smoke").documents[0]

    response = api_client.post(
        "/api/v1/documents",
        files={"file": (order.filename, order.path.read_bytes(), "application/pdf")},
        data={"doc_type": DOC_TYPE},
    )
    assert response.status_code == 201
    document_id = uuid.UUID(response.json()["id"])

    with Session(migrated_engine) as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.page_count == 2
        session.expunge(document)
    return document


@pytest.fixture
def extracted_po(po_document: Document, migrated_engine: Engine):
    def _extract(payload: dict | None = None, provider=None):
        provider = provider or RecordingProvider(payload)
        with Session(migrated_engine) as session:
            document = session.get(Document, po_document.id)
            extraction = extraction_service.extract_document(
                session, document, provider
            )
            session.expunge_all()
            return extraction.id, provider

    return _extract


def _fields(engine: Engine, extraction_id: uuid.UUID) -> dict[str, FieldValue]:
    with Session(engine) as session:
        extraction = session.get(Extraction, extraction_id)
        return {value.field_name: value for value in extraction.field_values}


# --- 1. schema ------------------------------------------------------------


def test_a_valid_purchase_order_parses() -> None:
    order = PurchaseOrder.model_validate(VALID_PO_PAYLOAD)

    assert order.purchase_order_number.value == "PO-2026-2001"
    assert order.order_date.value.isoformat() == "2026-02-03"
    assert order.vendor_name.value == "Acme Supplies BV"
    assert order.customer_name.value == "Beta Ltd"


def test_the_schema_covers_the_required_business_fields() -> None:
    required = {
        "purchase_order_number",
        "order_date",
        "vendor_name",
        "customer_name",
        "currency",
        "subtotal",
        "tax",
        "total",
        "line_items",
    }

    assert required <= set(PurchaseOrder.model_fields)


def test_every_field_carries_its_own_confidence() -> None:
    from app.extractors.base import ExtractedField

    order = PurchaseOrder.model_validate(VALID_PO_PAYLOAD)

    for field_name in PurchaseOrder.model_fields:
        assert isinstance(getattr(order, field_name), ExtractedField), field_name


def test_amounts_are_exact_decimals_not_floats() -> None:
    order = PurchaseOrder.model_validate(VALID_PO_PAYLOAD)

    assert isinstance(order.total.value, Decimal)
    assert order.total.value == Decimal("726.00")
    assert order.subtotal.value + order.tax.value == order.total.value


def test_a_missing_required_field_is_rejected() -> None:
    payload = {k: v for k, v in VALID_PO_PAYLOAD.items() if k != "total"}

    with pytest.raises(ValidationError):
        PurchaseOrder.model_validate(payload)


def test_an_extra_field_is_rejected() -> None:
    payload = dict(VALID_PO_PAYLOAD) | {
        "shipping_terms": {"value": "DDP", "confidence": 1.0}
    }

    with pytest.raises(ValidationError):
        PurchaseOrder.model_validate(payload)


def test_an_unparseable_money_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _parsed(total="six hundred")


def test_an_unparseable_date_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _parsed(order_date="the third of February")


def test_a_line_item_without_a_description_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _parsed(line_items=[{"quantity": "1", "unit_price": "1.00", "amount": "1.00"}])


def test_a_line_item_with_an_extra_column_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _parsed(line_items=[{"description": "X", "sku": "ABC-1"}])


def test_line_items_parse_into_typed_rows() -> None:
    order = PurchaseOrder.model_validate(VALID_PO_PAYLOAD)

    rows = order.line_items.value
    assert all(isinstance(row, PurchaseOrderLineItem) for row in rows)
    assert rows[0].amount == Decimal("400.00")


def test_money_uses_the_same_convention_as_the_invoice_schema() -> None:
    from app.extractors.base import Money
    from app.extractors.invoice import Money as InvoiceMoney

    assert Money is InvoiceMoney


# --- 2. extraction configuration ------------------------------------------


def test_the_purchase_order_type_is_registered() -> None:
    assert DOC_TYPE in registered_doc_types()
    assert registered_doc_types() == ("invoice", "purchase_order")


def test_the_registry_selects_the_purchase_order_configuration() -> None:
    extractor = get_extractor(DOC_TYPE)

    assert isinstance(extractor, PurchaseOrderExtractor)
    assert extractor.schema is PurchaseOrder
    assert extractor.prompt_version == PROMPT_VERSION == "purchase_order/v1"


def test_the_prompt_is_specific_to_purchase_orders() -> None:
    prompt = get_extractor(DOC_TYPE).prompt

    assert "purchase order" in prompt
    assert "the buyer is the sender" in prompt
    # The prompt must not ask the model to do the arithmetic.
    assert "Do not compute one from the others" in prompt


def test_the_invoice_configuration_is_untouched() -> None:
    extractor = get_extractor("invoice")

    assert extractor.schema is Invoice
    assert extractor.prompt_version == "invoice/v1"


def test_the_provider_receives_the_purchase_order_schema_and_prompt(
    extracted_po, migrated_engine: Engine
) -> None:
    _, provider = extracted_po()

    call = provider.calls[0]
    assert call["schema"] is PurchaseOrder
    assert call["prompt"] is get_extractor(DOC_TYPE).prompt
    assert [image.page_number for image in call["images"]] == [1, 2]


def test_extraction_records_the_purchase_order_prompt_version(
    extracted_po, migrated_engine: Engine
) -> None:
    extraction_id, _ = extracted_po()

    with Session(migrated_engine) as session:
        extraction = session.get(Extraction, extraction_id)

    assert extraction.prompt_version == "purchase_order/v1"
    assert extraction.model_name == "po-fake-1"
    assert extraction.input_tokens == 900
    assert extraction.cost_usd == Decimal("0.003600")
    assert extraction.latency_ms == 1400
    assert extraction.raw_response["id"] == "msg_po_fake"


def test_a_provider_failure_is_recorded_the_same_way_as_for_invoices(
    extracted_po, migrated_engine: Engine
) -> None:
    extraction_id, _ = extracted_po(
        provider=RecordingProvider(raises=ProviderError("the api fell over"))
    )

    with Session(migrated_engine) as session:
        extraction = session.get(Extraction, extraction_id)
        document = session.get(Document, extraction.document_id)

    assert document.status is DocumentStatus.FAILED
    assert "the api fell over" in extraction.error
    assert extraction.parsed is None


def test_the_upload_endpoint_accepts_the_new_doc_type(po_document: Document) -> None:
    assert po_document.doc_type == DOC_TYPE


def test_the_extract_endpoint_accepts_a_purchase_order(
    api_client: TestClient, po_document: Document, monkeypatch
) -> None:
    provider = RecordingProvider()
    monkeypatch.setattr(extraction_service, "get_provider", lambda: provider)

    response = api_client.post(f"/api/v1/documents/{po_document.id}/extract")

    assert response.status_code == 202
    assert response.json()["doc_type"] == DOC_TYPE
    assert len(provider.calls) == 1


# --- 3. deterministic validation ------------------------------------------


def test_the_validator_is_registered_for_the_new_type() -> None:
    assert get_validator(DOC_TYPE) is validate_purchase_order


def test_a_consistent_purchase_order_has_no_failures() -> None:
    assert validate_purchase_order(_parsed()).failures() == ()


def test_required_identifiers_must_not_be_blank() -> None:
    assert _status(_parsed(purchase_order_number="  "), "purchase_order_number.not_blank") is (
        CheckStatus.FAILED
    )
    assert _status(_parsed(), "vendor_name.not_blank") is CheckStatus.PASSED


def test_the_order_date_must_be_a_date() -> None:
    assert _status(_parsed(), "order_date.is_a_date") is CheckStatus.PASSED
    assert _status(_parsed(order_date=None), "order_date.is_a_date") is (
        CheckStatus.SKIPPED
    )


def test_delivery_must_not_precede_the_order() -> None:
    name = "dates.delivery_on_or_after_order"

    assert _status(_parsed(), name) is CheckStatus.PASSED
    assert _status(_parsed(delivery_date="2026-01-01"), name) is CheckStatus.FAILED
    assert _status(_parsed(delivery_date=None), name) is CheckStatus.SKIPPED


@pytest.mark.parametrize("code", ["EUR", "usd", "GBP"])
def test_a_supported_currency_passes(code: str) -> None:
    assert _status(_parsed(currency=code), "currency.iso_4217") is CheckStatus.PASSED


@pytest.mark.parametrize("bad", ["EURO", "XX", "€"])
def test_an_unsupported_currency_fails(bad: str) -> None:
    assert _status(_parsed(currency=bad), "currency.iso_4217") is CheckStatus.FAILED


def test_subtotal_plus_tax_must_equal_total() -> None:
    name = "totals.subtotal_plus_tax_equals_total"

    assert _status(_parsed(), name) is CheckStatus.PASSED
    assert _status(_parsed(total="900.00"), name) is CheckStatus.FAILED
    # The same one-cent tolerance the invoice rules use.
    assert _status(_parsed(total="726.01"), name) is CheckStatus.PASSED
    assert _status(_parsed(subtotal=None), name) is CheckStatus.SKIPPED


def test_line_items_must_sum_to_the_subtotal() -> None:
    name = "line_items.sum_to_subtotal"

    assert _status(_parsed(), name) is CheckStatus.PASSED
    assert _status(
        _parsed(subtotal="500.00", tax="105.00", total="605.00"), name
    ) is CheckStatus.FAILED


def test_row_arithmetic_is_checked() -> None:
    name = "line_items.row_quantity_times_price"

    assert _status(_parsed(), name) is CheckStatus.PASSED
    broken = _parsed(
        line_items=[
            {
                "description": "Widget, blue",
                "quantity": "10",
                "unit_price": "40.00",
                "amount": "999.00",
            }
        ]
    )
    assert _status(broken, name) is CheckStatus.FAILED


def test_arithmetic_that_cannot_run_is_skipped_not_failed() -> None:
    partial = _parsed(
        line_items=[{"description": "Consulting", "quantity": None,
                     "unit_price": None, "amount": None}]
    )

    assert _status(partial, "line_items.row_quantity_times_price") is (
        CheckStatus.SKIPPED
    )
    assert _status(partial, "line_items.sum_to_subtotal") is CheckStatus.SKIPPED


def test_the_shared_rules_are_the_invoice_rules_not_a_copy() -> None:
    """Both types compose the same functions from app/validation/common.py."""
    import inspect

    from app.validation import common, invoice, purchase_order

    for module in (invoice, purchase_order):
        source = inspect.getsource(module)
        assert "from app.validation.common import" in source
        # Neither module reimplements the arithmetic.
        assert "AMOUNT_TOLERANCE = Decimal" not in source
    assert "AMOUNT_TOLERANCE = Decimal" in inspect.getsource(common)


# --- 4. confidence --------------------------------------------------------


def test_purchase_order_fields_are_scored_by_the_shared_scorer(
    extracted_po, migrated_engine: Engine
) -> None:
    extraction_id, _ = extracted_po()

    fields = _fields(migrated_engine, extraction_id)
    assert set(fields) == set(PurchaseOrder.model_fields)
    for field in fields.values():
        assert field.validation is not None
        assert {signal["name"] for signal in field.validation["signals"]} <= {
            "model",
            "schema",
            "arithmetic",
            "text_layer",
        }


def test_the_model_confidence_is_preserved_for_purchase_orders(
    extracted_po, migrated_engine: Engine
) -> None:
    extraction_id, _ = extracted_po()

    fields = _fields(migrated_engine, extraction_id)
    assert fields["total"].model_confidence == pytest.approx(0.98)
    assert fields["total"].confidence != fields["total"].model_confidence


def test_a_failed_arithmetic_check_flags_the_purchase_order_field(
    extracted_po, migrated_engine: Engine
) -> None:
    extraction_id, _ = extracted_po(_confident(total="9999.99"))

    fields = _fields(migrated_engine, extraction_id)
    for field_name in ("subtotal", "tax", "total"):
        assert fields[field_name].needs_review is True, field_name
        assert (
            "totals.subtotal_plus_tax_equals_total"
            in fields[field_name].validation["blocking_failures"]
        )
    assert fields["vendor_name"].needs_review is False


def test_the_text_layer_signal_applies_to_purchase_orders(
    extracted_po, migrated_engine: Engine
) -> None:
    """The fixture PDF has a text layer, so the signal is available."""
    extraction_id, _ = extracted_po()

    signals = _fields(migrated_engine, extraction_id)["purchase_order_number"].validation
    assert any(signal["name"] == "text_layer" for signal in signals["signals"])


def test_the_configured_threshold_and_weights_are_unchanged() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.confidence_threshold == 0.85
    assert settings.confidence_weight_model == 0.40
    assert settings.confidence_weight_schema == 0.20
    assert settings.confidence_weight_arithmetic == 0.20
    assert settings.confidence_weight_text_layer == 0.20


# --- 5. review and corrections --------------------------------------------


def test_a_low_confidence_purchase_order_field_enters_the_review_queue(
    api_client: TestClient, extracted_po, migrated_engine: Engine
) -> None:
    payload = _confident()
    payload["customer_name"]["confidence"] = 0.1
    extracted_po(payload)

    queued = {item["field_name"] for item in api_client.get("/api/v1/review").json()["items"]}

    assert "customer_name" in queued


def test_the_queue_entry_carries_the_purchase_order_document(
    api_client: TestClient, extracted_po, po_document: Document
) -> None:
    extracted_po(_confident(total="9999.99"))

    item = next(
        item
        for item in api_client.get("/api/v1/review").json()["items"]
        if item["field_name"] == "total"
    )

    assert item["document"]["doc_type"] == DOC_TYPE
    assert item["document"]["id"] == str(po_document.id)
    assert "totals.subtotal_plus_tax_equals_total" in item["failed_checks"]


def test_correcting_a_purchase_order_field_uses_the_shared_workflow(
    api_client: TestClient, extracted_po, migrated_engine: Engine
) -> None:
    extracted_po(_confident(total="9999.99"))
    item = next(
        item
        for item in api_client.get("/api/v1/review").json()["items"]
        if item["field_name"] == "total"
    )

    response = api_client.post(
        f"/api/v1/review/{item['field_id']}", json={"corrected_value": "726.00"}
    )

    assert response.status_code == 201
    with Session(migrated_engine) as session:
        field = session.get(FieldValue, uuid.UUID(item["field_id"]))
        assert field.value == "726.00"
        assert field.needs_review is False
        assert field.is_corrected is True
        # The model's numbers are still the model's.
        assert field.model_confidence == pytest.approx(0.99)


def test_the_purchase_order_correction_audit_trail_is_preserved(
    api_client: TestClient, extracted_po, migrated_engine: Engine
) -> None:
    extracted_po(_confident(total="9999.99"))
    item = next(
        item
        for item in api_client.get("/api/v1/review").json()["items"]
        if item["field_name"] == "total"
    )
    api_client.post(
        f"/api/v1/review/{item['field_id']}", json={"corrected_value": "726.00"}
    )

    with Session(migrated_engine) as session:
        correction = session.execute(select(Correction)).scalar_one()

    assert correction.original_value == "9999.99"
    assert correction.corrected_value == "726.00"
    assert correction.corrected_at is not None


def test_the_document_becomes_reviewed_once_every_field_is_handled(
    api_client: TestClient, extracted_po, po_document: Document,
    migrated_engine: Engine
) -> None:
    extracted_po(_confident(total="9999.99"))

    while queue := api_client.get("/api/v1/review").json()["items"]:
        api_client.post(
            f"/api/v1/review/{queue[0]['field_id']}",
            json={"corrected_value": "corrected"},
        )

    with Session(migrated_engine) as session:
        assert session.get(Document, po_document.id).status is DocumentStatus.REVIEWED


def test_the_review_page_renders_purchase_order_fields(
    api_client: TestClient, extracted_po
) -> None:
    extracted_po(_confident(total="9999.99"))

    html = api_client.get("/review").text

    assert "purchase_order" in html
    assert "totals.subtotal_plus_tax_equals_total" in html


# --- 6. export ------------------------------------------------------------


def test_json_export_works_for_purchase_orders(
    api_client: TestClient, extracted_po, po_document: Document
) -> None:
    extracted_po()

    body = api_client.get(f"/api/v1/documents/{po_document.id}/export").json()

    assert body["document"]["doc_type"] == DOC_TYPE
    assert set(body["fields"]) == set(PurchaseOrder.model_fields)
    assert body["fields"]["purchase_order_number"]["value"] == "PO-2026-2001"
    assert body["extraction"]["prompt_version"] == "purchase_order/v1"


def test_json_export_nests_purchase_order_line_items(
    api_client: TestClient, extracted_po, po_document: Document
) -> None:
    """Which field is structured is read off the PO schema, not hard-coded."""
    extracted_po()

    rows = api_client.get(
        f"/api/v1/documents/{po_document.id}/export"
    ).json()["fields"]["line_items"]["value"]

    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["description"] == "Widget, blue"
    assert rows[0]["amount"] == "400.00"


def test_json_export_shows_a_corrected_purchase_order_value(
    api_client: TestClient, extracted_po, po_document: Document
) -> None:
    extracted_po(_confident(total="9999.99"))
    item = next(
        item
        for item in api_client.get("/api/v1/review").json()["items"]
        if item["field_name"] == "total"
    )
    api_client.post(
        f"/api/v1/review/{item['field_id']}", json={"corrected_value": "726.00"}
    )

    field = api_client.get(
        f"/api/v1/documents/{po_document.id}/export"
    ).json()["fields"]["total"]

    assert field["value"] == "726.00"
    assert field["corrected"] is True
    assert field["original_value"] == "9999.99"


def test_csv_export_works_for_purchase_orders(
    api_client: TestClient, extracted_po, po_document: Document
) -> None:
    import csv
    import io

    extracted_po()

    response = api_client.get(
        f"/api/v1/documents/{po_document.id}/export", params={"format": "csv"}
    )

    assert response.status_code == 200
    rows = {row["field_name"]: row for row in csv.DictReader(io.StringIO(response.text))}
    assert set(rows) == set(PurchaseOrder.model_fields)
    assert rows["total"]["value"] == "726.00"
    assert rows["purchase_order_number"]["doc_type"] == DOC_TYPE
    parsed = json.loads(rows["line_items"]["value"])
    assert [row["description"] for row in parsed] == ["Widget, blue", "Widget, red"]


# --- 7. the evaluation harness carries the new type -----------------------


def test_the_smoke_dataset_declares_its_document_type() -> None:
    loaded = dataset_module.load("purchase_orders_smoke")

    assert loaded.doc_type == DOC_TYPE
    assert len(loaded) == 3
    assert loaded.synthetic is True
    assert "not a benchmark" in loaded.description


def test_the_invoice_dataset_still_resolves_to_invoices() -> None:
    assert dataset_module.load("invoices_v1").doc_type == "invoice"
    assert len(dataset_module.load("invoices_v1")) == 20


def test_the_smoke_labels_are_arithmetically_consistent() -> None:
    for document in dataset_module.load("purchase_orders_smoke").documents:
        fields = document.fields
        rows = sum(
            (Decimal(row["amount"]) for row in fields["line_items"]), Decimal(0)
        )
        assert rows == Decimal(fields["subtotal"]), document.document_id
        assert Decimal(fields["subtotal"]) + Decimal(fields["tax"]) == Decimal(
            fields["total"]
        ), document.document_id


def test_the_runner_evaluates_purchase_orders(
    migrated_engine: Engine, storage_dir
) -> None:
    from evals import runner

    loaded = dataset_module.load("purchase_orders_smoke")

    class LabelEcho:
        model_name = "po-label-echo"

        def __init__(self, dataset):
            self._remaining = list(dataset.documents)

        def extract(self, images, schema, prompt):
            assert schema is PurchaseOrder
            labelled = self._remaining.pop(0)
            payload = {
                name: {"value": value, "confidence": 0.99, "source_page": 1}
                for name, value in labelled.fields.items()
            }
            content = json.dumps(payload)
            return RawExtraction(
                content=content,
                raw_response={
                    "id": "echo",
                    "model": self.model_name,
                    "content": [{"type": "text", "text": content}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 800, "output_tokens": 150},
                },
                model_name=self.model_name,
                input_tokens=800,
                output_tokens=150,
                cost_usd=Decimal("0.003000"),
                latency_ms=1200,
            )

    with Session(migrated_engine) as session:
        report = runner.evaluate(session, loaded, LabelEcho(loaded))

    assert report.documents == 3
    assert report.fields_evaluated == 30
    assert report.overall_accuracy == 1.0
    assert report.prompt_version == "purchase_order/v1"
    assert report.failed_documents == ()


def test_the_stub_provider_adapts_to_the_purchase_order_schema(
    migrated_engine: Engine, storage_dir
) -> None:
    """The stub must not assume invoices, and must not invent values."""
    from evals import runner
    from evals.providers import StubProvider

    loaded = dataset_module.load("purchase_orders_smoke")

    with Session(migrated_engine) as session:
        report = runner.evaluate(session, loaded, StubProvider())

    assert report.failed_documents == ()
    assert report.overall_accuracy == 0.0
    assert report.mean_cost_usd is None


# --- 8. the invoice path is unchanged -------------------------------------


def test_the_invoice_schema_is_unchanged() -> None:
    """The Money type moved to a shared module; the schema must not have."""
    import hashlib

    from app.providers.anthropic_vision import json_schema_for

    rendered = json.dumps(json_schema_for(Invoice), sort_keys=True)

    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        "b9b1fbad64014893d2d2b5c43f74a6b0edd67d5816c8ad4b14cf90b6882e364d"
    )


def test_the_invoice_check_names_are_unchanged() -> None:
    from tests.conftest import VALID_INVOICE_PAYLOAD

    report = get_validator("invoice")(Invoice.model_validate(VALID_INVOICE_PAYLOAD))

    assert {check.name for check in report.checks} == {
        "invoice_number.not_blank",
        "vendor_name.not_blank",
        "currency.not_blank",
        "invoice_date.is_a_date",
        "due_date.is_a_date",
        "dates.due_on_or_after_invoice",
        "currency.iso_4217",
        "totals.subtotal_plus_tax_equals_total",
        "line_items.sum_to_subtotal",
        "line_items.row_quantity_times_price",
    }


def test_the_two_document_types_do_not_share_a_prompt_version() -> None:
    assert (
        get_extractor("invoice").prompt_version
        != get_extractor(DOC_TYPE).prompt_version
    )


def test_the_default_model_is_unchanged() -> None:
    from app.config import Settings

    assert Settings(_env_file=None).extraction_model == "claude-sonnet-5"


def test_the_core_pipeline_does_not_name_a_document_type() -> None:
    """The proof that this is one pipeline and not two."""
    import inspect

    from app import confidence, corrections, export, extraction, review

    for module in (extraction, confidence, review, corrections, export):
        source = inspect.getsource(module)
        assert 'doc_type == "' not in source, module.__name__
        assert "purchase_order" not in source, module.__name__
