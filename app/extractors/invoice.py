"""Invoice extraction: the schema, and the prompt that asks for it.

Adding another document type means adding a module like this one and
registering it — no pipeline change. See `app/extractors/__init__.py`.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, WithJsonSchema

from app.extractors.base import ExtractedField

PROMPT_VERSION = "invoice/v1"

# Amounts travel as decimal strings. Pydantic parses a string into Decimal
# happily, and asking for a string keeps the model away from float rounding —
# a JSON number would arrive as a float and 1234.56 would stop being exact.
# The default Decimal schema is a regex with a lookahead, which schema
# validators cannot be relied on to support, so the shape is stated directly.
Money = Annotated[
    Decimal,
    WithJsonSchema(
        {
            "type": "string",
            "description": 'Decimal amount as a plain string, e.g. "1234.56".',
        }
    ),
]


class InvoiceLineItem(BaseModel):
    """One row of the invoice's line-item table."""

    model_config = ConfigDict(extra="forbid")

    description: str
    quantity: Money | None = None
    unit_price: Money | None = None
    amount: Money | None = None


class Invoice(BaseModel):
    """The strict target schema for an invoice.

    Every field is wrapped in `ExtractedField`, so a value never arrives without
    the model's confidence and the page it came from. Nothing here is validated
    for arithmetic consistency — totals arithmetic, date parsing and currency
    checks are deterministic work that belongs in `app/validation/`
    (milestone 5), not in the schema and not in the prompt.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity — the three fields v1 acceptance is measured on are
    # invoice_number, invoice_date and total.
    invoice_number: ExtractedField[str]
    invoice_date: ExtractedField[date]
    due_date: ExtractedField[date]

    # Who issued it and who owes.
    vendor_name: ExtractedField[str]
    vendor_address: ExtractedField[str]
    vendor_tax_id: ExtractedField[str]
    customer_name: ExtractedField[str]
    purchase_order_number: ExtractedField[str]

    # Money. Decimal, never float — these are amounts, not measurements, and
    # milestone 5 has to add them up exactly.
    currency: ExtractedField[str]
    subtotal: ExtractedField[Money]
    tax: ExtractedField[Money]
    total: ExtractedField[Money]

    # Kept so milestone 5 can check that the rows sum to the subtotal.
    line_items: ExtractedField[list[InvoiceLineItem]]


PROMPT = """\
You are reading a commercial invoice. The images are the pages of a single \
invoice, in order, page 1 first.

Extract the fields defined by the response schema. For every field:

- `value`: what the document actually says. Use null if the invoice does not \
contain that field. Do not infer, calculate, or fill in a plausible value.
- `confidence`: how sure you are that `value` is exactly what the document \
says, from 0 to 1. Use a low confidence when the text is unclear, ambiguous, \
or you had to choose between candidates. A confident wrong answer is the worst \
outcome, so report uncertainty honestly.
- `source_page`: the 1-based page number you read the value from, or null if \
the field is absent.

Field notes:

- `invoice_number` is the vendor's own identifier for this invoice, not an \
account, order, or customer number.
- Dates must be ISO 8601 (YYYY-MM-DD). If a date is ambiguous between \
day-first and month-first, pick the reading the document's other dates \
support and lower the confidence.
- `currency` is the ISO 4217 code (for example EUR, USD, GBP). Infer it from a \
currency symbol only when the symbol is unambiguous.
- Amounts are plain decimal numbers with no currency symbol, no thousands \
separators, and `.` as the decimal separator. Keep a credit or negative amount \
negative.
- `subtotal`, `tax` and `total` must each be copied from the document. Do not \
compute one from the others; if the invoice does not state it, use null.
- `line_items` is every row of the line-item table, in document order. Report \
the row's own figures; leave a cell null when the row does not show it.

Return only the structured response.\
"""


class InvoiceExtractor:
    doc_type = "invoice"
    prompt_version = PROMPT_VERSION
    schema = Invoice
    prompt = PROMPT
