"""The strict invoice schema."""

import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.extractors import Invoice, InvoiceLineItem, get_extractor
from app.extractors.base import ExtractedField
from app.extractors.invoice import PROMPT_VERSION

from .conftest import VALID_INVOICE_PAYLOAD

ACCEPTANCE_FIELDS = {"invoice_number", "invoice_date", "total"}


def test_every_field_reports_its_own_confidence() -> None:
    """A value must never arrive without the model's confidence in it."""
    invoice = Invoice.model_validate(VALID_INVOICE_PAYLOAD)

    for field_name in Invoice.model_fields:
        field = getattr(invoice, field_name)
        assert isinstance(field, ExtractedField), field_name
        assert 0.0 <= field.confidence <= 1.0


def test_schema_covers_the_v1_acceptance_fields() -> None:
    assert ACCEPTANCE_FIELDS <= set(Invoice.model_fields)


def test_parses_typed_values() -> None:
    invoice = Invoice.model_validate(VALID_INVOICE_PAYLOAD)

    assert invoice.invoice_number.value == "INV-2026-0042"
    assert invoice.invoice_date.value == date(2026, 1, 5)
    assert invoice.currency.value == "EUR"
    assert invoice.due_date.value == date(2026, 2, 4)


def test_amounts_stay_exact_decimals() -> None:
    """Money must not become a float — milestone 5 has to add these up."""
    invoice = Invoice.model_validate(VALID_INVOICE_PAYLOAD)

    assert invoice.total.value == Decimal("1210.00")
    assert isinstance(invoice.total.value, Decimal)
    assert invoice.subtotal.value + invoice.tax.value == invoice.total.value


def test_line_items_parse_into_typed_rows() -> None:
    invoice = Invoice.model_validate(VALID_INVOICE_PAYLOAD)

    rows = invoice.line_items.value
    assert rows is not None
    assert len(rows) == 2
    assert all(isinstance(row, InvoiceLineItem) for row in rows)
    assert rows[0].description == "Widget, blue"
    assert rows[0].amount == Decimal("400.00")


def test_source_page_is_captured() -> None:
    invoice = Invoice.model_validate(VALID_INVOICE_PAYLOAD)

    assert invoice.invoice_number.source_page == 1
    assert invoice.total.source_page == 2


def test_absent_field_is_null_not_invented() -> None:
    invoice = Invoice.model_validate(VALID_INVOICE_PAYLOAD)

    assert invoice.purchase_order_number.value is None
    assert invoice.purchase_order_number.source_page is None


def test_unknown_field_is_rejected() -> None:
    """Strict means strict: the model cannot add fields of its own."""
    payload = dict(VALID_INVOICE_PAYLOAD) | {
        "invented_field": {"value": "x", "confidence": 1.0}
    }

    with pytest.raises(ValidationError):
        Invoice.model_validate(payload)


def test_missing_field_is_rejected() -> None:
    payload = {k: v for k, v in VALID_INVOICE_PAYLOAD.items() if k != "total"}

    with pytest.raises(ValidationError):
        Invoice.model_validate(payload)


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_outside_zero_to_one_is_rejected(confidence: float) -> None:
    payload = json.loads(json.dumps(VALID_INVOICE_PAYLOAD))
    payload["total"]["confidence"] = confidence

    with pytest.raises(ValidationError):
        Invoice.model_validate(payload)


def test_unparseable_date_is_rejected() -> None:
    payload = json.loads(json.dumps(VALID_INVOICE_PAYLOAD))
    payload["invoice_date"]["value"] = "the fifth of January"

    with pytest.raises(ValidationError):
        Invoice.model_validate(payload)


def test_extractor_exposes_schema_prompt_and_version() -> None:
    extractor = get_extractor("invoice")

    assert extractor.doc_type == "invoice"
    assert extractor.schema is Invoice
    assert extractor.prompt_version == PROMPT_VERSION
    assert extractor.prompt.strip()


def test_prompt_does_not_ask_the_model_to_do_the_arithmetic() -> None:
    """Totals checking is deterministic work for milestone 5, not prompt work."""
    prompt = get_extractor("invoice").prompt

    assert "Do not compute one from the others" in prompt
    assert "calculate" in prompt  # ...as an instruction *not* to
