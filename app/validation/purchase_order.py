"""Deterministic checks for an extracted purchase order.

Composed from the shared rules in `app/validation/common.py` — the arithmetic
a purchase order states is the same arithmetic an invoice states, so it is
checked by the same code. What is specific to a purchase order is which
identifiers must not be blank, and that a delivery date cannot fall before the
order date.
"""

from typing import Any

from app.validation.base import ValidationReport
from app.validation.common import (
    currency_check,
    date_order_check,
    is_a_date_checks,
    line_item_checks,
    not_blank_checks,
    totals_checks,
)

TEXT_FIELDS_THAT_MUST_NOT_BE_BLANK = (
    "purchase_order_number",
    "vendor_name",
    "currency",
)
DATE_FIELDS = ("order_date", "delivery_date")


def validate_purchase_order(parsed: Any) -> ValidationReport:
    """Run every applicable check over a parsed purchase order."""
    checks = [
        *not_blank_checks(parsed, TEXT_FIELDS_THAT_MUST_NOT_BE_BLANK),
        *is_a_date_checks(parsed, DATE_FIELDS),
        *date_order_check(
            parsed,
            name="dates.delivery_on_or_after_order",
            earlier="order_date",
            later="delivery_date",
        ),
        *currency_check(parsed),
        *totals_checks(parsed),
        *line_item_checks(parsed),
    ]
    return ValidationReport(tuple(checks))
