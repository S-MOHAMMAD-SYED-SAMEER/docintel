"""Deterministic checks for an extracted invoice.

Nothing here asks the model anything. Every rule is arithmetic, a date, or a
code lookup — if it can be checked in Python, it is checked in Python, so a
confident wrong answer still fails.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.validation.base import (
    CheckKind,
    ValidationReport,
    failed,
    passed,
    skipped,
)
from app.validation.currencies import ISO_4217_CODES

# Money is compared to the cent. Per-row rounding means a line-item sum can
# legitimately land a cent away from the stated subtotal.
AMOUNT_TOLERANCE = Decimal("0.01")

TEXT_FIELDS_THAT_MUST_NOT_BE_BLANK = ("invoice_number", "vendor_name", "currency")


def validate_invoice(parsed: Any) -> ValidationReport:
    """Run every applicable check over a parsed invoice."""
    checks = [
        *_text_checks(parsed),
        *_date_checks(parsed),
        *_currency_checks(parsed),
        *_totals_checks(parsed),
        *_line_item_checks(parsed),
    ]
    return ValidationReport(tuple(checks))


def _value(parsed: Any, field_name: str) -> Any:
    field = getattr(parsed, field_name, None)
    return getattr(field, "value", None)


# --- schema-kind checks ---------------------------------------------------


def _text_checks(parsed: Any):
    for field_name in TEXT_FIELDS_THAT_MUST_NOT_BE_BLANK:
        value = _value(parsed, field_name)
        name = f"{field_name}.not_blank"
        if value is None:
            yield skipped(name, CheckKind.SCHEMA, [field_name], "not extracted")
        elif not str(value).strip():
            yield failed(
                name, CheckKind.SCHEMA, [field_name], "value is blank or whitespace"
            )
        else:
            yield passed(name, CheckKind.SCHEMA, [field_name])


def _date_checks(parsed: Any):
    invoice_date = _value(parsed, "invoice_date")
    due_date = _value(parsed, "due_date")

    for field_name, value in (("invoice_date", invoice_date), ("due_date", due_date)):
        name = f"{field_name}.is_a_date"
        if value is None:
            yield skipped(name, CheckKind.SCHEMA, [field_name], "not extracted")
        elif isinstance(value, date):
            yield passed(name, CheckKind.SCHEMA, [field_name], value.isoformat())
        else:
            yield failed(
                name,
                CheckKind.SCHEMA,
                [field_name],
                f"{value!r} is not a calendar date",
            )

    name = "dates.due_on_or_after_invoice"
    fields = ("invoice_date", "due_date")
    if not (isinstance(invoice_date, date) and isinstance(due_date, date)):
        yield skipped(name, CheckKind.SCHEMA, fields, "both dates are required")
    elif due_date < invoice_date:
        yield failed(
            name,
            CheckKind.SCHEMA,
            fields,
            f"due {due_date.isoformat()} precedes invoice {invoice_date.isoformat()}",
        )
    else:
        yield passed(name, CheckKind.SCHEMA, fields)


def _currency_checks(parsed: Any):
    value = _value(parsed, "currency")
    name = "currency.iso_4217"
    if value is None:
        yield skipped(name, CheckKind.SCHEMA, ["currency"], "not extracted")
        return

    code = str(value).strip().upper()
    if code in ISO_4217_CODES:
        yield passed(name, CheckKind.SCHEMA, ["currency"], code)
    else:
        yield failed(
            name,
            CheckKind.SCHEMA,
            ["currency"],
            f"{value!r} is not an ISO 4217 currency code",
        )


# --- arithmetic-kind checks -----------------------------------------------


def _amount(parsed: Any, field_name: str) -> Decimal | None:
    return _as_decimal(_value(parsed, field_name))


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _close(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= AMOUNT_TOLERANCE


def _totals_checks(parsed: Any):
    subtotal = _amount(parsed, "subtotal")
    tax = _amount(parsed, "tax")
    total = _amount(parsed, "total")

    name = "totals.subtotal_plus_tax_equals_total"
    fields = ("subtotal", "tax", "total")
    if subtotal is None or tax is None or total is None:
        yield skipped(
            name,
            CheckKind.ARITHMETIC,
            fields,
            "subtotal, tax and total must all be present",
        )
        return

    expected = subtotal + tax
    if _close(expected, total):
        yield passed(name, CheckKind.ARITHMETIC, fields, f"{subtotal} + {tax} = {total}")
    else:
        yield failed(
            name,
            CheckKind.ARITHMETIC,
            fields,
            f"{subtotal} + {tax} = {expected}, but total says {total}",
        )


def _line_item_checks(parsed: Any):
    rows = _value(parsed, "line_items")
    subtotal = _amount(parsed, "subtotal")

    sum_name = "line_items.sum_to_subtotal"
    sum_fields = ("line_items", "subtotal")
    row_name = "line_items.row_quantity_times_price"

    if not rows:
        yield skipped(sum_name, CheckKind.ARITHMETIC, sum_fields, "no line items")
        yield skipped(row_name, CheckKind.ARITHMETIC, ["line_items"], "no line items")
        return

    amounts = [_as_decimal(getattr(row, "amount", None)) for row in rows]
    if subtotal is None:
        yield skipped(sum_name, CheckKind.ARITHMETIC, sum_fields, "no subtotal stated")
    elif any(amount is None for amount in amounts):
        yield skipped(
            sum_name,
            CheckKind.ARITHMETIC,
            sum_fields,
            "at least one line item has no amount",
        )
    else:
        total_of_rows = sum(amounts, Decimal(0))
        if _close(total_of_rows, subtotal):
            yield passed(
                sum_name,
                CheckKind.ARITHMETIC,
                sum_fields,
                f"{len(amounts)} row(s) sum to {total_of_rows}",
            )
        else:
            yield failed(
                sum_name,
                CheckKind.ARITHMETIC,
                sum_fields,
                f"rows sum to {total_of_rows}, but subtotal says {subtotal}",
            )

    yield from _row_arithmetic(rows, row_name)


def _row_arithmetic(rows: list[Any], name: str):
    """quantity x unit_price = amount, for every row that states all three."""
    checkable = []
    for index, row in enumerate(rows, start=1):
        quantity = _as_decimal(getattr(row, "quantity", None))
        unit_price = _as_decimal(getattr(row, "unit_price", None))
        amount = _as_decimal(getattr(row, "amount", None))
        if quantity is None or unit_price is None or amount is None:
            continue
        checkable.append((index, quantity * unit_price, amount))

    if not checkable:
        yield skipped(
            name,
            CheckKind.ARITHMETIC,
            ["line_items"],
            "no row states quantity, unit price and amount",
        )
        return

    mismatches = [
        f"row {index}: expected {expected}, stated {amount}"
        for index, expected, amount in checkable
        if not _close(expected, amount)
    ]
    if mismatches:
        yield failed(name, CheckKind.ARITHMETIC, ["line_items"], "; ".join(mismatches))
    else:
        yield passed(
            name,
            CheckKind.ARITHMETIC,
            ["line_items"],
            f"{len(checkable)} row(s) check out",
        )
