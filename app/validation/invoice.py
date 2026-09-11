"""Deterministic checks for an extracted invoice.

Nothing here asks the model anything. Every rule is arithmetic, a date, or a
code lookup — if it can be checked in Python, it is checked in Python, so a
confident wrong answer still fails.

The arithmetic and format rules an invoice shares with every other business
document live in `app/validation/common.py`. What remains here is what is
specific to an invoice: which identifiers must not be blank, and that a due
date cannot precede the invoice date.
"""

from typing import Any

from app.validation.base import ValidationReport
from app.validation.common import (
    AMOUNT_TOLERANCE,
    currency_check,
    date_order_check,
    is_a_date_checks,
    line_item_checks,
    not_blank_checks,
    totals_checks,
)

__all__ = ["AMOUNT_TOLERANCE", "TEXT_FIELDS_THAT_MUST_NOT_BE_BLANK", "validate_invoice"]

TEXT_FIELDS_THAT_MUST_NOT_BE_BLANK = ("invoice_number", "vendor_name", "currency")
DATE_FIELDS = ("invoice_date", "due_date")


def validate_invoice(parsed: Any) -> ValidationReport:
    """Run every applicable check over a parsed invoice."""
    checks = [
        *not_blank_checks(parsed, TEXT_FIELDS_THAT_MUST_NOT_BE_BLANK),
        *is_a_date_checks(parsed, DATE_FIELDS),
        *date_order_check(
            parsed,
            name="dates.due_on_or_after_invoice",
            earlier="invoice_date",
            later="due_date",
        ),
        *currency_check(parsed),
        *totals_checks(parsed),
        *line_item_checks(parsed),
    ]
    return ValidationReport(tuple(checks))
