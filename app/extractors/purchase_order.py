"""Purchase-order extraction: the schema, and the prompt that asks for it.

A sibling of `invoice.py`, not a special case of it. The pipeline reaches this
module only through the registry in `app/extractors/__init__.py`; nothing in
the extraction, confidence, review, correction or export code names a purchase
order.

A purchase order is what a buyer sends a supplier: the buyer's own order
number, what was ordered, and what it is expected to cost. It reads much like
an invoice in reverse, which is exactly why it shares the pipeline.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict

from app.extractors.base import ExtractedField, Money

PROMPT_VERSION = "purchase_order/v1"


class PurchaseOrderLineItem(BaseModel):
    """One ordered line.

    Deliberately its own type rather than a reuse of `InvoiceLineItem`: the two
    happen to carry the same columns today, and tying a purchase order's shape
    to an invoice's would make changing either one a change to both. The
    arithmetic over these rows is shared — see `app/validation/common.py`.
    """

    model_config = ConfigDict(extra="forbid")

    description: str
    quantity: Money | None = None
    unit_price: Money | None = None
    amount: Money | None = None


class PurchaseOrder(BaseModel):
    """The strict target schema for a purchase order.

    Every field is an `ExtractedField`, so a value never arrives without the
    model's confidence and the page it came from. Nothing is checked for
    arithmetic consistency here — that is deterministic work for
    `app/validation/`, not for the schema and not for the prompt.
    """

    model_config = ConfigDict(extra="forbid")

    # The buyer's own reference for this order.
    purchase_order_number: ExtractedField[str]
    order_date: ExtractedField[date]
    # When the buyer expects delivery. Often absent; when present it must not
    # fall before the order date.
    delivery_date: ExtractedField[date]

    vendor_name: ExtractedField[str]
    customer_name: ExtractedField[str]

    # Money. Decimal, never float — these are amounts, not measurements, and
    # the validation layer has to add them up exactly.
    currency: ExtractedField[str]
    subtotal: ExtractedField[Money]
    tax: ExtractedField[Money]
    total: ExtractedField[Money]

    line_items: ExtractedField[list[PurchaseOrderLineItem]]


PROMPT = """\
You are reading a purchase order — the document a buyer sends a supplier to \
order goods or services. The images are the pages of a single purchase order, \
in order, page 1 first.

Extract the fields defined by the response schema. For every field:

- `value`: what the document actually says. Use null if the purchase order \
does not contain that field. Do not infer, calculate, or fill in a plausible \
value.
- `confidence`: how sure you are that `value` is exactly what the document \
says, from 0 to 1. Use a low confidence when the text is unclear, ambiguous, \
or you had to choose between candidates. A confident wrong answer is the worst \
outcome, so report uncertainty honestly.
- `source_page`: the 1-based page number you read the value from, or null if \
the field is absent.

Field notes:

- `purchase_order_number` is the buyer's own identifier for this order, often \
labelled PO number or order number. It is not a quote, contract, invoice or \
account number.
- `vendor_name` is the supplier the order is addressed to. `customer_name` is \
the buyer issuing it. Do not swap them: on a purchase order the buyer is the \
sender.
- Dates must be ISO 8601 (YYYY-MM-DD). `order_date` is when the order was \
raised; `delivery_date` is the requested or promised delivery date, null if \
the document does not state one. If a date is ambiguous between day-first and \
month-first, pick the reading the document's other dates support and lower the \
confidence.
- `currency` is the ISO 4217 code (for example EUR, USD, GBP). Infer it from a \
currency symbol only when the symbol is unambiguous.
- Amounts are plain decimal numbers with no currency symbol, no thousands \
separators, and `.` as the decimal separator. Keep a credit or negative amount \
negative.
- `subtotal`, `tax` and `total` must each be copied from the document. Do not \
compute one from the others; if the purchase order does not state it, use null.
- `line_items` is every row of the ordered-items table, in document order. \
Report the row's own figures; leave a cell null when the row does not show it.

Return only the structured response.\
"""


class PurchaseOrderExtractor:
    doc_type = "purchase_order"
    prompt_version = PROMPT_VERSION
    schema = PurchaseOrder
    prompt = PROMPT
