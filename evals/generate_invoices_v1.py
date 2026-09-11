"""Build the synthetic `invoices_v1` dataset.

Run with `python -m evals.generate_invoices_v1`. The committed dataset is the
artefact the harness evaluates; this script exists so it can be regenerated and
reviewed, not so it runs at eval time.

The invoice data below is the single source of truth: each record is rendered
into a PDF *and* written to `labels.json`. The labels therefore describe what
the document says, independently of anything a model later claims it says.
"""

import json
from decimal import Decimal
from pathlib import Path

DATASET_NAME = "invoices_v1"
DATASET_DIR = Path(__file__).resolve().parent / "datasets" / DATASET_NAME

VENDORS = [
    ("Acme Supplies BV", "12 Kade, 1011 AB Amsterdam", "NL123456789B01", "EUR"),
    ("Northwind Trading Ltd", "4 Dock Road, Bristol BS1 4RN", "GB987654321", "GBP"),
    ("Kestrel Print & Sign", "88 Mill Lane, Leeds LS1 5QR", "GB223344556", "GBP"),
    ("Bluegrass Hardware Inc", "900 Sycamore St, Austin TX 78701", "US45-6789012", "USD"),
    ("Meridian Logistics SA", "7 Rue Carnot, 69002 Lyon", "FR40123456824", "EUR"),
]

CUSTOMERS = [
    "Beta Ltd",
    "Corvus Studio",
    "Hollis & Dane LLP",
    "Riverbend Cafe",
    "Tallow Works",
]

CATALOGUE = [
    ("Widget, blue", "40.00"),
    ("Widget, red", "40.00"),
    ("Hex bolt M8 (box of 100)", "12.50"),
    ("Courier delivery", "18.00"),
    ("Laminated sign, A2", "65.00"),
    ("Consulting, hourly", "95.00"),
    ("Packing crate, large", "27.25"),
]

TAX_RATES = ["0.21", "0.20", "0.00", "0.0825", "0.19"]


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def build_invoices() -> list[dict[str, object]]:
    """Twenty deterministic invoices. No randomness, no clock, no network."""
    invoices: list[dict[str, object]] = []

    for index in range(20):
        number = index + 1
        vendor_name, vendor_address, vendor_tax_id, currency = VENDORS[index % 5]
        customer = CUSTOMERS[index % 5]

        # Two or three rows, walking the catalogue so every invoice differs.
        row_count = 2 + (index % 2)
        rows = []
        for offset in range(row_count):
            description, unit_price = CATALOGUE[(index + offset) % len(CATALOGUE)]
            quantity = Decimal(1 + ((index + offset) % 9))
            amount = quantity * Decimal(unit_price)
            rows.append(
                {
                    "description": description,
                    "quantity": f"{quantity}",
                    "unit_price": unit_price,
                    "amount": _money(amount),
                }
            )

        subtotal = sum((Decimal(row["amount"]) for row in rows), Decimal(0))
        tax_rate = Decimal(TAX_RATES[index % 5])
        tax = (subtotal * tax_rate).quantize(Decimal("0.01"))
        total = subtotal + tax

        issued_day = 1 + (index % 28)
        issued_month = 1 + (index % 12)
        due_month = issued_month if issued_day <= 14 else (issued_month % 12) + 1
        due_day = issued_day + 14 if issued_day <= 14 else issued_day - 14

        invoices.append(
            {
                "document_id": f"invoice-{number:03d}",
                "filename": f"invoice-{number:03d}.pdf",
                "fields": {
                    "invoice_number": f"INV-2026-{1000 + number}",
                    "invoice_date": f"2026-{issued_month:02d}-{issued_day:02d}",
                    "due_date": f"2026-{due_month:02d}-{due_day:02d}",
                    "vendor_name": vendor_name,
                    "vendor_address": vendor_address,
                    "vendor_tax_id": vendor_tax_id,
                    "customer_name": customer,
                    "purchase_order_number": (
                        f"PO-{4400 + number}" if number % 3 else None
                    ),
                    "currency": currency,
                    "subtotal": _money(subtotal),
                    "tax": _money(tax),
                    "total": _money(total),
                    "line_items": rows,
                },
            }
        )

    return invoices


def render_pages(invoice: dict[str, object]) -> list[str]:
    """The invoice as page text. This is what the PDF will say."""
    fields = invoice["fields"]
    purchase_order = fields["purchase_order_number"]

    header = [
        f"{fields['vendor_name']}",
        f"{fields['vendor_address']}",
        f"VAT {fields['vendor_tax_id']}",
        "",
        f"INVOICE {fields['invoice_number']}",
        f"Invoice date {fields['invoice_date']}",
        f"Due date {fields['due_date']}",
        "",
        f"Bill to: {fields['customer_name']}",
    ]
    if purchase_order is not None:
        header.append(f"Purchase order: {purchase_order}")

    table = ["Description                     Qty   Unit price     Amount"]
    for row in fields["line_items"]:
        table.append(
            f"{row['description']:<30}  {row['quantity']:>3}   "
            f"{row['unit_price']:>10}   {row['amount']:>10}"
        )
    table += [
        "",
        f"Subtotal   {fields['currency']} {fields['subtotal']}",
        f"Tax        {fields['currency']} {fields['tax']}",
        f"TOTAL      {fields['currency']} {fields['total']}",
    ]

    return ["\n".join(header), "\n".join(table)]


def build_pdf(pages: list[str]) -> bytes:
    """A minimal, valid PDF whose pages carry a real text layer."""

    def escape(text: str) -> str:
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    page_count = len(pages)
    page_ids = [4 + 2 * index for index in range(page_count)]
    content_ids = [5 + 2 * index for index in range(page_count)]

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{pid} 0 R" for pid in page_ids)
            + f"] /Count {page_count} >>"
        ).encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    }

    for index, text in enumerate(pages):
        body = ["BT", "/F1 10 Tf", "13 TL", "40 750 Td"]
        body += [f"({escape(line)}) Tj T*" for line in (text.splitlines() or [""])]
        body.append("ET")
        stream = "\n".join(body).encode()
        objects[content_ids[index]] = (
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        objects[page_ids[index]] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 792] "
            "/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_ids[index]} 0 R >>"
        ).encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"

    xref_at = len(out)
    highest = max(objects)
    out += f"xref\n0 {highest + 1}\n".encode() + b"0000000000 65535 f \n"
    for number in range(1, highest + 1):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {highest + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n"
    ).encode() + b"%%EOF\n"
    return bytes(out)


def main() -> None:
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    invoices = build_invoices()

    for invoice in invoices:
        pdf = build_pdf(render_pages(invoice))
        (DATASET_DIR / str(invoice["filename"])).write_bytes(pdf)

    labels = {
        "dataset": DATASET_NAME,
        "synthetic": True,
        "description": (
            "Twenty synthetic invoices generated by "
            "evals/generate_invoices_v1.py. Vendors, customers and amounts are "
            "invented; no real invoice is included. Accuracy measured on this "
            "dataset says how the pipeline handles clean, born-digital "
            "invoices and must not be quoted as real-world accuracy."
        ),
        "documents": [
            {
                "document_id": invoice["document_id"],
                "filename": invoice["filename"],
                "fields": invoice["fields"],
            }
            for invoice in invoices
        ],
    }
    (DATASET_DIR / "labels.json").write_text(
        json.dumps(labels, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"Wrote {len(invoices)} documents and labels.json to {DATASET_DIR}")


if __name__ == "__main__":
    main()
