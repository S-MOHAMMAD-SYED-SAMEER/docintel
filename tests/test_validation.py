"""Deterministic invoice checks — no model, no database, no I/O."""

import json
from decimal import Decimal

import pytest

from app.extractors import Invoice
from app.validation import CheckKind, CheckStatus, get_validator, no_checks
from app.validation.currencies import ISO_4217_CODES
from app.validation.invoice import AMOUNT_TOLERANCE, validate_invoice

from .conftest import VALID_INVOICE_PAYLOAD


def _invoice(**overrides: object) -> Invoice:
    """The valid fixture with individual field values replaced."""
    payload = json.loads(json.dumps(VALID_INVOICE_PAYLOAD))
    for field_name, value in overrides.items():
        payload[field_name]["value"] = value
    return Invoice.model_validate(payload)


def _status(invoice: Invoice, check_name: str) -> CheckStatus:
    report = validate_invoice(invoice)
    check = next(check for check in report.checks if check.name == check_name)
    return check.status


# --- dates ----------------------------------------------------------------


def test_valid_dates_pass() -> None:
    invoice = _invoice()

    assert _status(invoice, "invoice_date.is_a_date") is CheckStatus.PASSED
    assert _status(invoice, "due_date.is_a_date") is CheckStatus.PASSED
    assert _status(invoice, "dates.due_on_or_after_invoice") is CheckStatus.PASSED


def test_due_date_before_invoice_date_fails() -> None:
    invoice = _invoice(due_date="2025-12-01")

    assert _status(invoice, "dates.due_on_or_after_invoice") is CheckStatus.FAILED


def test_same_day_due_date_passes() -> None:
    invoice = _invoice(due_date="2026-01-05")

    assert _status(invoice, "dates.due_on_or_after_invoice") is CheckStatus.PASSED


def test_missing_date_skips_rather_than_fails() -> None:
    """An invoice with no due date has not got its dates wrong."""
    invoice = _invoice(due_date=None)

    assert _status(invoice, "due_date.is_a_date") is CheckStatus.SKIPPED
    assert _status(invoice, "dates.due_on_or_after_invoice") is CheckStatus.SKIPPED


def test_an_unparseable_date_never_reaches_validation() -> None:
    """The schema rejects it first — that is the type/format layer doing its job."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _invoice(invoice_date="the fifth of January")


# --- currency -------------------------------------------------------------


@pytest.mark.parametrize("code", ["EUR", "USD", "GBP", "JPY"])
def test_valid_currency_passes(code: str) -> None:
    assert _status(_invoice(currency=code), "currency.iso_4217") is CheckStatus.PASSED


def test_lowercase_currency_is_accepted() -> None:
    assert _status(_invoice(currency="eur"), "currency.iso_4217") is CheckStatus.PASSED


@pytest.mark.parametrize("code", ["EURO", "XX", "€", "BITCOIN", "ZZZ"])
def test_invalid_currency_fails(code: str) -> None:
    assert _status(_invoice(currency=code), "currency.iso_4217") is CheckStatus.FAILED


def test_missing_currency_skips() -> None:
    assert _status(_invoice(currency=None), "currency.iso_4217") is CheckStatus.SKIPPED


def test_currency_list_looks_like_iso_4217() -> None:
    assert "EUR" in ISO_4217_CODES
    assert len(ISO_4217_CODES) > 150
    assert all(len(code) == 3 and code.isupper() for code in ISO_4217_CODES)


# --- blank text -----------------------------------------------------------


def test_blank_required_text_fails() -> None:
    """Pydantic accepts an empty string; a blank invoice number is still wrong."""
    assert _status(_invoice(invoice_number="   "), "invoice_number.not_blank") is (
        CheckStatus.FAILED
    )


def test_present_text_passes() -> None:
    assert _status(_invoice(), "vendor_name.not_blank") is CheckStatus.PASSED


# --- totals arithmetic ----------------------------------------------------

TOTALS = "totals.subtotal_plus_tax_equals_total"


def test_subtotal_plus_tax_equals_total_passes() -> None:
    assert _status(_invoice(), TOTALS) is CheckStatus.PASSED


def test_totals_mismatch_fails() -> None:
    assert _status(_invoice(total="1500.00"), TOTALS) is CheckStatus.FAILED


def test_totals_mismatch_reports_the_numbers() -> None:
    report = validate_invoice(_invoice(total="1500.00"))
    check = next(check for check in report.checks if check.name == TOTALS)

    assert "1210.00" in check.detail
    assert "1500.00" in check.detail


def test_a_rounding_cent_is_tolerated() -> None:
    assert AMOUNT_TOLERANCE == Decimal("0.01")
    assert _status(_invoice(total="1210.01"), TOTALS) is CheckStatus.PASSED


def test_two_cents_is_not_tolerated() -> None:
    assert _status(_invoice(total="1210.02"), TOTALS) is CheckStatus.FAILED


@pytest.mark.parametrize("missing", ["subtotal", "tax", "total"])
def test_totals_skip_when_a_value_is_missing(missing: str) -> None:
    """Arithmetic that cannot be performed is not arithmetic that failed."""
    assert _status(_invoice(**{missing: None}), TOTALS) is CheckStatus.SKIPPED


def test_negative_amounts_are_handled() -> None:
    """A credit note states negative figures and must still add up."""
    invoice = _invoice(subtotal="-1000.00", tax="-210.00", total="-1210.00")

    assert _status(invoice, TOTALS) is CheckStatus.PASSED


# --- line-item arithmetic -------------------------------------------------

SUM_TO_SUBTOTAL = "line_items.sum_to_subtotal"
ROW_MATHS = "line_items.row_quantity_times_price"


def test_line_items_summing_to_subtotal_passes() -> None:
    assert _status(_invoice(), SUM_TO_SUBTOTAL) is CheckStatus.PASSED


def test_line_items_not_summing_to_subtotal_fails() -> None:
    invoice = _invoice(subtotal="900.00", tax="210.00", total="1110.00")

    assert _status(invoice, SUM_TO_SUBTOTAL) is CheckStatus.FAILED


def test_row_quantity_times_price_passes() -> None:
    assert _status(_invoice(), ROW_MATHS) is CheckStatus.PASSED


def test_row_arithmetic_mismatch_fails() -> None:
    invoice = _invoice(
        line_items=[
            {
                "description": "Widget, blue",
                "quantity": "10",
                "unit_price": "40.00",
                "amount": "999.00",
            }
        ]
    )

    assert _status(invoice, ROW_MATHS) is CheckStatus.FAILED


def test_row_arithmetic_skips_rows_missing_a_figure() -> None:
    invoice = _invoice(
        line_items=[
            {"description": "Consulting", "quantity": None, "unit_price": None,
             "amount": "1000.00"}
        ]
    )

    assert _status(invoice, ROW_MATHS) is CheckStatus.SKIPPED
    # The sum against the subtotal can still be checked.
    assert _status(invoice, SUM_TO_SUBTOTAL) is CheckStatus.PASSED


def test_sum_skips_when_a_row_has_no_amount() -> None:
    invoice = _invoice(
        line_items=[
            {"description": "Widget", "quantity": "1", "unit_price": "10.00",
             "amount": None}
        ]
    )

    assert _status(invoice, SUM_TO_SUBTOTAL) is CheckStatus.SKIPPED


def test_sum_skips_without_a_subtotal() -> None:
    assert _status(_invoice(subtotal=None), SUM_TO_SUBTOTAL) is CheckStatus.SKIPPED


def test_no_line_items_skips_both_row_checks() -> None:
    invoice = _invoice(line_items=[])

    assert _status(invoice, SUM_TO_SUBTOTAL) is CheckStatus.SKIPPED
    assert _status(invoice, ROW_MATHS) is CheckStatus.SKIPPED


# --- report shape ---------------------------------------------------------


def test_a_valid_invoice_has_no_failures() -> None:
    assert validate_invoice(_invoice()).failures() == ()


def test_checks_name_the_fields_they_bear_on() -> None:
    report = validate_invoice(_invoice(total="1500.00"))

    failed_names = {check.name for check in report.failures()}
    assert TOTALS in failed_names
    # A cross-field check taints all three of its fields.
    for field_name in ("subtotal", "tax", "total"):
        assert report.status_for(field_name, CheckKind.ARITHMETIC) is CheckStatus.FAILED


def test_a_field_with_no_checks_reports_skipped() -> None:
    report = validate_invoice(_invoice())

    assert report.status_for("vendor_address", CheckKind.SCHEMA) is CheckStatus.SKIPPED
    assert report.status_for("customer_name", CheckKind.ARITHMETIC) is (
        CheckStatus.SKIPPED
    )


def test_one_failure_outweighs_other_passes_for_the_same_field() -> None:
    """currency passes `not_blank` but fails `iso_4217`; the field still fails."""
    report = validate_invoice(_invoice(currency="EURO"))

    assert report.status_for("currency", CheckKind.SCHEMA) is CheckStatus.FAILED


def test_validation_is_deterministic() -> None:
    invoice = _invoice()

    assert validate_invoice(invoice).as_list() == validate_invoice(invoice).as_list()


def test_registry_returns_the_invoice_validator() -> None:
    assert get_validator("invoice") is validate_invoice


def test_unknown_doc_type_runs_no_checks() -> None:
    """An unregistered type has no rules — that is neither a pass nor a fail."""
    validator = get_validator("bill_of_lading")

    assert validator is no_checks
    assert validator(_invoice()).checks == ()
