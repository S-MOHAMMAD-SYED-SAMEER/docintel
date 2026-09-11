"""Deterministic checks that hold for any business document.

An invoice and a purchase order state the same arithmetic: rows carry a
quantity, a unit price and an amount; the rows sum to a subtotal; subtotal plus
tax is the total. That logic lives here once, parameterised by field name, so a
new document type composes it rather than copying it.

What stays in a document type's own module is only what is genuinely specific
to it — which identifiers must not be blank, and which of its dates must not
precede which.
"""

from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.validation.base import Check, CheckKind, failed, passed, skipped
from app.validation.currencies import ISO_4217_CODES

# Money is compared to the cent. Per-row rounding means a line-item sum can
# legitimately land a cent away from the stated subtotal.
AMOUNT_TOLERANCE = Decimal("0.01")


def value_of(parsed: Any, field_name: str) -> Any:
    """The extracted value of a field, or None if the field is not there."""
    field = getattr(parsed, field_name, None)
    return getattr(field, "value", None)


def as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def amount_of(parsed: Any, field_name: str) -> Decimal | None:
    return as_decimal(value_of(parsed, field_name))


def close(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= AMOUNT_TOLERANCE


# --- schema-kind checks ---------------------------------------------------


def not_blank_checks(parsed: Any, field_names: Iterable[str]) -> Iterator[Check]:
    """`<field>.not_blank` — Pydantic accepts "", a blank identifier is wrong."""
    for field_name in field_names:
        value = value_of(parsed, field_name)
        name = f"{field_name}.not_blank"
        if value is None:
            yield skipped(name, CheckKind.SCHEMA, [field_name], "not extracted")
        elif not str(value).strip():
            yield failed(
                name, CheckKind.SCHEMA, [field_name], "value is blank or whitespace"
            )
        else:
            yield passed(name, CheckKind.SCHEMA, [field_name])


def is_a_date_checks(parsed: Any, field_names: Iterable[str]) -> Iterator[Check]:
    """`<field>.is_a_date` — the value parsed into a real calendar date."""
    for field_name in field_names:
        value = value_of(parsed, field_name)
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


def date_order_check(
    parsed: Any, *, name: str, earlier: str, later: str
) -> Iterator[Check]:
    """`later` must not fall before `earlier`, when both are present."""
    earlier_value = value_of(parsed, earlier)
    later_value = value_of(parsed, later)
    fields = (earlier, later)

    if not (isinstance(earlier_value, date) and isinstance(later_value, date)):
        yield skipped(name, CheckKind.SCHEMA, fields, "both dates are required")
    elif later_value < earlier_value:
        yield failed(
            name,
            CheckKind.SCHEMA,
            fields,
            f"{later} {later_value.isoformat()} precedes "
            f"{earlier} {earlier_value.isoformat()}",
        )
    else:
        yield passed(name, CheckKind.SCHEMA, fields)


def currency_check(parsed: Any, field_name: str = "currency") -> Iterator[Check]:
    """`currency.iso_4217` — a real currency code, not a symbol or a word."""
    value = value_of(parsed, field_name)
    name = f"{field_name}.iso_4217"
    if value is None:
        yield skipped(name, CheckKind.SCHEMA, [field_name], "not extracted")
        return

    code = str(value).strip().upper()
    if code in ISO_4217_CODES:
        yield passed(name, CheckKind.SCHEMA, [field_name], code)
    else:
        yield failed(
            name,
            CheckKind.SCHEMA,
            [field_name],
            f"{value!r} is not an ISO 4217 currency code",
        )


# --- arithmetic-kind checks -----------------------------------------------


def totals_checks(
    parsed: Any,
    *,
    subtotal_field: str = "subtotal",
    tax_field: str = "tax",
    total_field: str = "total",
) -> Iterator[Check]:
    """`totals.subtotal_plus_tax_equals_total`."""
    subtotal = amount_of(parsed, subtotal_field)
    tax = amount_of(parsed, tax_field)
    total = amount_of(parsed, total_field)

    name = "totals.subtotal_plus_tax_equals_total"
    fields = (subtotal_field, tax_field, total_field)
    if subtotal is None or tax is None or total is None:
        yield skipped(
            name,
            CheckKind.ARITHMETIC,
            fields,
            f"{subtotal_field}, {tax_field} and {total_field} must all be present",
        )
        return

    expected = subtotal + tax
    if close(expected, total):
        yield passed(
            name, CheckKind.ARITHMETIC, fields, f"{subtotal} + {tax} = {total}"
        )
    else:
        yield failed(
            name,
            CheckKind.ARITHMETIC,
            fields,
            f"{subtotal} + {tax} = {expected}, but {total_field} says {total}",
        )


def line_item_checks(
    parsed: Any,
    *,
    rows_field: str = "line_items",
    subtotal_field: str = "subtotal",
) -> Iterator[Check]:
    """`line_items.sum_to_subtotal` and `line_items.row_quantity_times_price`."""
    rows = value_of(parsed, rows_field)
    subtotal = amount_of(parsed, subtotal_field)

    sum_name = f"{rows_field}.sum_to_subtotal"
    sum_fields = (rows_field, subtotal_field)
    row_name = f"{rows_field}.row_quantity_times_price"

    if not rows:
        yield skipped(sum_name, CheckKind.ARITHMETIC, sum_fields, "no line items")
        yield skipped(row_name, CheckKind.ARITHMETIC, [rows_field], "no line items")
        return

    amounts = [as_decimal(getattr(row, "amount", None)) for row in rows]
    if subtotal is None:
        yield skipped(
            sum_name,
            CheckKind.ARITHMETIC,
            sum_fields,
            f"no {subtotal_field} stated",
        )
    elif any(amount is None for amount in amounts):
        yield skipped(
            sum_name,
            CheckKind.ARITHMETIC,
            sum_fields,
            "at least one line item has no amount",
        )
    else:
        total_of_rows = sum(amounts, Decimal(0))
        if close(total_of_rows, subtotal):
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
                f"rows sum to {total_of_rows}, but {subtotal_field} says {subtotal}",
            )

    yield from row_arithmetic_check(rows, name=row_name, rows_field=rows_field)


def row_arithmetic_check(
    rows: list[Any], *, name: str, rows_field: str = "line_items"
) -> Iterator[Check]:
    """quantity x unit_price = amount, for every row that states all three."""
    checkable = []
    for index, row in enumerate(rows, start=1):
        quantity = as_decimal(getattr(row, "quantity", None))
        unit_price = as_decimal(getattr(row, "unit_price", None))
        amount = as_decimal(getattr(row, "amount", None))
        if quantity is None or unit_price is None or amount is None:
            continue
        checkable.append((index, quantity * unit_price, amount))

    if not checkable:
        yield skipped(
            name,
            CheckKind.ARITHMETIC,
            [rows_field],
            "no row states quantity, unit price and amount",
        )
        return

    mismatches = [
        f"row {index}: expected {expected}, stated {amount}"
        for index, expected, amount in checkable
        if not close(expected, amount)
    ]
    if mismatches:
        yield failed(name, CheckKind.ARITHMETIC, [rows_field], "; ".join(mismatches))
    else:
        yield passed(
            name,
            CheckKind.ARITHMETIC,
            [rows_field],
            f"{len(checkable)} row(s) check out",
        )
