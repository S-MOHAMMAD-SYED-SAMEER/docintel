"""Build the synthetic `invoices_hard_v1` dataset: ten invoices that are harder to read than `invoices_v1`.

Run with `python -m evals.generate_invoices_hard_v1`. The committed dataset is the artefact the harness
evaluates; this script exists so it can be regenerated and reviewed, not so it runs at eval time. (The JPEG and
PNG encoders can differ a little between Pillow versions, so a regeneration need not reproduce the committed
bytes; the committed bytes are what is frozen and hashed.)

The invoice data below is the single source of truth: each record is rendered into a page image or a PDF
*and* written to `labels.json`. The labels therefore describe what the document says, independently of anything
a model later claims it says. Every figure is internally consistent (rows sum to the subtotal, subtotal + tax =
total) unless the document is a credit note, where every amount is negative.

What makes these harder than `invoices_v1`, which is ten-point Courier text with a clean text layer:
  * most documents are IMAGES with no text layer (a scan or a photograph), so the text-layer check never
    corroborates a value and the model has to read pixels;
  * skew, blur, noise, low resolution, JPEG compression, a shadow, a stamp over a figure, a scribble;
  * other layouts and wording (the totals first, "Bill to" next to a different "Ship to", an account number next
    to the invoice number);
  * European number and date formats, a currency symbol with no code, a day-first date that only the other date
    on the page disambiguates, negative amounts, a missing due date, tax of zero;
  * a longer table across two pages with the totals on page two.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from evals.generate_invoices_v1 import build_pdf

DATASET_NAME = "invoices_hard_v1"
DATASET_DIR = Path(__file__).resolve().parent / "datasets" / DATASET_NAME

PAGE = (1240, 1754)  # A4 at 150 dpi


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _rows(items: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    """`(description, quantity, unit_price)` to labelled rows with the amount computed."""
    return [
        {
            "description": description,
            "quantity": quantity,
            "unit_price": unit_price,
            "amount": _money(Decimal(quantity) * Decimal(unit_price)),
        }
        for description, quantity, unit_price in items
    ]


def _totals(rows: list[dict[str, str]], rate: str) -> tuple[str, str, str]:
    subtotal = sum((Decimal(r["amount"]) for r in rows), Decimal(0))
    tax = (subtotal * Decimal(rate)).quantize(Decimal("0.01"))
    return _money(subtotal), _money(tax), _money(subtotal + tax)


# --------------------------------------------------------------------------------------------- the ten records


def records() -> list[dict]:
    out: list[dict] = []

    def add(number: int, filename_ext: str, kind: str, why: str, fields: dict, **style) -> None:
        out.append(
            {
                "document_id": f"hard-{number:03d}",
                "filename": f"hard-{number:03d}.{filename_ext}",
                "kind": kind,
                "why_hard": why,
                "style": style,
                "fields": fields,
            }
        )

    # 1. A skewed, noisy scan of an ordinary invoice.
    rows = _rows([("Hex bolt M8 (box of 100)", "6", "12.50"), ("Laminated sign, A2", "2", "65.00")])
    sub, tax, total = _totals(rows, "0.20")
    add(1, "png", "scan", "Skewed 2.5 degrees, blurred and noisy; image only (no text layer).", {
        "invoice_number": "INV-2026-2201", "invoice_date": "2026-02-09", "due_date": "2026-02-23",
        "vendor_name": "Kestrel Print & Sign", "vendor_address": "88 Mill Lane, Leeds LS1 5QR",
        "vendor_tax_id": "GB223344556", "customer_name": "Riverbend Cafe", "purchase_order_number": "PO-7781",
        "currency": "GBP", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, degrade="scan", angle=2.5, blur=1.3, noise=40, layout="standard", date_fmt="iso", money_fmt="plain", symbol=False)

    # 2. European formats and a currency symbol with no code.
    rows = _rows([("Beratung, Stunden", "12", "95.00"), ("Reisekosten", "1", "184.50")])
    sub, tax, total = _totals(rows, "0.19")
    add(2, "png", "clean-image", "Day-first dotted dates, decimal commas and thousands dots, a euro sign and no currency code; image only.", {
        "invoice_number": "RE-2026-0457", "invoice_date": "2026-03-04", "due_date": "2026-04-03",
        "vendor_name": "Lindenhof Beratung GmbH", "vendor_address": "Goethestrasse 14, 80336 Munchen",
        "vendor_tax_id": "DE811234567", "customer_name": "Tallow Works", "purchase_order_number": None,
        "currency": "EUR", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, degrade="light", angle=0.0, blur=0.0, noise=0, layout="standard", date_fmt="dotted", money_fmt="european", symbol=True)

    # 3. An ambiguous date that only the other date on the page settles.
    rows = _rows([("Widget, blue", "9", "40.00"), ("Courier delivery", "1", "18.00")])
    sub, tax, total = _totals(rows, "0.21")
    add(3, "pdf", "text-pdf", "04/03/2026 could be April 3 or March 4; the due date 18/03/2026 shows it is day-first. Text layer present.", {
        "invoice_number": "INV-A-31207", "invoice_date": "2026-03-04", "due_date": "2026-03-18",
        "vendor_name": "Meridian Logistics SA", "vendor_address": "7 Rue Carnot, 69002 Lyon",
        "vendor_tax_id": "FR40123456824", "customer_name": "Corvus Studio", "purchase_order_number": "PO-5519",
        "currency": "EUR", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, layout="totals-first", date_fmt="slash-dmy", money_fmt="plain", symbol=False)

    # 4. A credit note: every amount negative.
    rows = _rows([("Widget, red", "-3", "40.00"), ("Laminated sign, A2", "-1", "65.00")])
    sub, tax, total = _totals(rows, "0.20")
    add(4, "pdf", "text-pdf", "A credit note: negative quantities and amounts, shown in brackets; the label keeps the sign. Text layer present.", {
        "invoice_number": "CN-2026-0098", "invoice_date": "2026-01-27", "due_date": "2026-02-10",
        "vendor_name": "Northwind Trading Ltd", "vendor_address": "4 Dock Road, Bristol BS1 4RN",
        "vendor_tax_id": "GB987654321", "customer_name": "Beta Ltd", "purchase_order_number": "PO-6120",
        "currency": "GBP", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, layout="standard", date_fmt="iso", money_fmt="brackets", symbol=False, credit=True)

    # 5. A low-resolution photograph: compression, shadow, slight perspective.
    rows = _rows([("Widget, blue", "4", "40.00"), ("Hex bolt M8 (box of 100)", "3", "12.50"), ("Courier delivery", "1", "18.00")])
    sub, tax, total = _totals(rows, "0.08")
    add(5, "jpg", "photo", "A low-resolution, heavily compressed photograph with a shadow across the lower half; image only.", {
        "invoice_number": "INV-2026-3340", "invoice_date": "2026-05-18", "due_date": "2026-06-01",
        "vendor_name": "Bluegrass Hardware Inc", "vendor_address": "900 Sycamore St, Austin TX 78701",
        "vendor_tax_id": "US45-6789012", "customer_name": "Hollis & Dane LLP", "purchase_order_number": "PO-9902",
        "currency": "USD", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, degrade="photo", angle=-1.5, blur=0.8, noise=22, layout="standard", date_fmt="iso", money_fmt="plain", symbol=False)

    # 6. Eight rows over two pages, totals on page two.
    rows = _rows([
        ("Widget, blue", "10", "40.00"), ("Widget, red", "5", "40.00"), ("Hex bolt M8 (box of 100)", "20", "12.50"),
        ("Courier delivery", "2", "18.00"), ("Laminated sign, A2", "3", "65.00"), ("Widget, blue", "7", "40.00"),
        ("Hex bolt M8 (box of 100)", "11", "12.50"), ("Laminated sign, A2", "1", "65.00"),
    ])
    sub, tax, total = _totals(rows, "0.21")
    add(6, "pdf", "text-pdf", "Eight line items run onto a second page and the totals are only on page two. Text layer present.", {
        "invoice_number": "INV-2026-4412", "invoice_date": "2026-06-30", "due_date": "2026-07-30",
        "vendor_name": "Acme Supplies BV", "vendor_address": "12 Kade, 1011 AB Amsterdam",
        "vendor_tax_id": "NL123456789B01", "customer_name": "Corvus Studio", "purchase_order_number": "PO-8873",
        "currency": "EUR", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, layout="long", date_fmt="iso", money_fmt="plain", symbol=False)

    # 7. A shipping row, and the totals block before the details.
    rows = _rows([("Widget, red", "12", "40.00"), ("Laminated sign, A2", "2", "65.00"), ("Freight and handling", "1", "42.00")])
    sub, tax, total = _totals(rows, "0.20")
    add(7, "pdf", "text-pdf", "A freight row inside the table, and a 'Total due' line that comes before the details. Text layer present.", {
        "invoice_number": "INV-77-0192", "invoice_date": "2026-07-14", "due_date": "2026-08-13",
        "vendor_name": "Kestrel Print & Sign", "vendor_address": "88 Mill Lane, Leeds LS1 5QR",
        "vendor_tax_id": "GB223344556", "customer_name": "Tallow Works", "purchase_order_number": "PO-3358",
        "currency": "GBP", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, layout="totals-first", date_fmt="iso", money_fmt="plain", symbol=False)

    # 8. Distractors: an order number, an account number, a different ship-to, a payment-processor header.
    rows = _rows([("Hex bolt M8 (box of 100)", "15", "12.50"), ("Widget, blue", "5", "40.00")])
    sub, tax, total = _totals(rows, "0.10")
    add(8, "png", "clean-image", "An order number and an account number beside the invoice number, a Ship-to that differs from the Bill-to, and a payment-processor line at the top; image only.", {
        "invoice_number": "9-208841", "invoice_date": "2026-08-21", "due_date": "2026-09-20",
        "vendor_name": "Meridian Logistics SA", "vendor_address": "7 Rue Carnot, 69002 Lyon",
        "vendor_tax_id": "FR40123456824", "customer_name": "Riverbend Cafe", "purchase_order_number": "PO-1042",
        "currency": "EUR", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, degrade="light", angle=0.0, blur=0.0, noise=0, layout="distractors", date_fmt="iso", money_fmt="plain", symbol=False)

    # 9. Fields that are genuinely absent, and a tax of zero.
    rows = _rows([("Widget, blue", "6", "40.00"), ("Courier delivery", "1", "18.00")])
    sub, tax, total = _totals(rows, "0")
    add(9, "pdf", "text-pdf", "No due date ('payable on receipt'), no tax id, no purchase order, and a tax of 0.00 (reverse charge). Text layer present.", {
        "invoice_number": "INV-2026-5005", "invoice_date": "2026-09-02", "due_date": None,
        "vendor_name": "Northwind Trading Ltd", "vendor_address": "4 Dock Road, Bristol BS1 4RN",
        "vendor_tax_id": None, "customer_name": "Hollis & Dane LLP", "purchase_order_number": None,
        "currency": "GBP", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, layout="sparse", date_fmt="iso", money_fmt="plain", symbol=False)

    # 10. A stamp over the total, a scribble, a coffee-ring smudge, a dense small font.
    rows = _rows([("Widget, red", "8", "40.00"), ("Hex bolt M8 (box of 100)", "4", "12.50"), ("Laminated sign, A2", "1", "65.00")])
    sub, tax, total = _totals(rows, "0.21")
    add(10, "png", "scan", "A semi-transparent PAID stamp across the total, a pen scribble, a smudge and a small dense font; image only.", {
        "invoice_number": "INV-2026-6120", "invoice_date": "2026-10-05", "due_date": "2026-11-04",
        "vendor_name": "Acme Supplies BV", "vendor_address": "12 Kade, 1011 AB Amsterdam",
        "vendor_tax_id": "NL123456789B01", "customer_name": "Beta Ltd", "purchase_order_number": "PO-2210",
        "currency": "EUR", "subtotal": sub, "tax": tax, "total": total, "line_items": rows,
    }, degrade="stamp", angle=-0.8, blur=0.8, noise=26, layout="standard", date_fmt="iso", money_fmt="plain", symbol=False, small=True)
    return out


# ---------------------------------------------------------------------------------------------- page text


def _fmt_date(iso: str | None, mode: str) -> str:
    if iso is None:
        return ""
    year, month, day = iso.split("-")
    return {
        "iso": iso,
        "dotted": f"{day}.{month}.{year}",
        "slash-dmy": f"{day}/{month}/{year}",
    }[mode]


def _fmt_money(value: str, mode: str, currency: str, symbol: bool) -> str:
    amount = Decimal(value)
    negative = amount < 0
    digits = f"{abs(amount):,.2f}"
    if mode == "european":
        digits = digits.replace(",", "X").replace(".", ",").replace("X", ".")
    if mode == "brackets" and negative:
        text = f"({digits})"
    else:
        text = f"-{digits}" if negative else digits
    if symbol:
        sign = {"EUR": "€", "GBP": "£", "USD": "$"}[currency]
        return f"{text} {sign}" if mode == "european" else f"{sign}{text}"
    return text


def _qty(value: str) -> str:
    return value


def lines_for(record: dict) -> list[str]:
    """The page text, one list of lines for all pages, with `\\f` marking a page break."""
    f = record["fields"]
    s = record["style"]
    date = lambda iso: _fmt_date(iso, s["date_fmt"])  # noqa: E731
    money = lambda v: _fmt_money(v, s["money_fmt"], f["currency"], s["symbol"])  # noqa: E731
    cur = "" if s["symbol"] else f"{f['currency']} "
    layout = s["layout"]
    L: list[str] = []

    def table(rows: list[dict[str, str]]) -> list[str]:
        out = ["Description                      Qty    Unit price      Amount"]
        for row in rows:
            out.append(
                f"{row['description']:<30}  {_qty(row['quantity']):>4}  {money(row['unit_price']):>11}  {money(row['amount']):>11}"
            )
        return out

    if layout == "standard":
        L += [f["vendor_name"], f["vendor_address"]]
        if f["vendor_tax_id"]:
            L.append(f"VAT {f['vendor_tax_id']}")
        L += ["", "CREDIT NOTE " + f["invoice_number"] if s.get("credit") else f"INVOICE {f['invoice_number']}"]
        L.append(f"Date {date(f['invoice_date'])}" if s["date_fmt"] != "dotted" else f"Rechnungsdatum {date(f['invoice_date'])}")
        if f["due_date"]:
            L.append(f"Due {date(f['due_date'])}" if s["date_fmt"] != "dotted" else f"Zahlbar bis {date(f['due_date'])}")
        L += ["", f"Bill to: {f['customer_name']}"]
        if f["purchase_order_number"]:
            L.append(f"Your ref: {f['purchase_order_number']}")
        L += [""] + table(f["line_items"]) + [""]
        L += [f"Subtotal    {cur}{money(f['subtotal'])}", f"Tax         {cur}{money(f['tax'])}", f"TOTAL       {cur}{money(f['total'])}"]
    elif layout == "totals-first":
        L += [f"TOTAL DUE  {cur}{money(f['total'])}", ""]
        L += [f["vendor_name"], f["vendor_address"]]
        if f["vendor_tax_id"]:
            L.append(f"Tax ID {f['vendor_tax_id']}")
        L += ["", f"Invoice no. {f['invoice_number']}", f"Issued {date(f['invoice_date'])}"]
        if f["due_date"]:
            L.append(f"Payment due {date(f['due_date'])}")
        L += [f"Customer: {f['customer_name']}"]
        if f["purchase_order_number"]:
            L.append(f"PO {f['purchase_order_number']}")
        L += [""] + table(f["line_items"]) + [""]
        L += [f"Net {cur}{money(f['subtotal'])}", f"VAT {cur}{money(f['tax'])}", f"Gross {cur}{money(f['total'])}"]
    elif layout == "long":
        L += [f["vendor_name"], f["vendor_address"], f"VAT {f['vendor_tax_id']}", "", f"INVOICE {f['invoice_number']}",
              f"Invoice date {date(f['invoice_date'])}", f"Due date {date(f['due_date'])}", "", f"Bill to: {f['customer_name']}",
              f"Purchase order: {f['purchase_order_number']}", ""]
        rows = f["line_items"]
        L += table(rows[:5]) + ["", "(continued overleaf)", "\f"]
        L += [f"INVOICE {f['invoice_number']}  page 2", ""] + table(rows[5:]) + [""]
        L += [f"Subtotal   {cur}{money(f['subtotal'])}", f"Tax        {cur}{money(f['tax'])}", f"TOTAL      {cur}{money(f['total'])}"]
    elif layout == "distractors":
        L += ["Payments processed by Paylink Services Ltd", "", f["vendor_name"], f["vendor_address"], f"VAT {f['vendor_tax_id']}", "",
              f"INVOICE NUMBER {f['invoice_number']}", "Order number 55-3319-A", "Account number 000-481-2207",
              f"Invoice date {date(f['invoice_date'])}", f"Due date {date(f['due_date'])}", "",
              f"Bill to: {f['customer_name']}", "Ship to: Tallow Works, 3 Kiln Row, Derby DE1 2AB",
              f"Purchase order: {f['purchase_order_number']}", ""] + table(f["line_items"]) + [""]
        L += [f"Subtotal   {cur}{money(f['subtotal'])}", f"Tax        {cur}{money(f['tax'])}", f"TOTAL      {cur}{money(f['total'])}"]
    elif layout == "sparse":
        L += [f["vendor_name"], f["vendor_address"], "", f"INVOICE {f['invoice_number']}", f"Date {date(f['invoice_date'])}",
              "Payable on receipt", "", f"Bill to: {f['customer_name']}", "", "Reverse charge: VAT to be accounted for by the recipient", ""]
        L += table(f["line_items"]) + [""]
        L += [f"Subtotal   {cur}{money(f['subtotal'])}", f"Tax        {cur}{money(f['tax'])}", f"TOTAL      {cur}{money(f['total'])}"]
    return L


# ---------------------------------------------------------------------------------------------- image rendering


#: A font that has the euro and pound signs (Pillow's built-in font does not draw the euro sign). The first one
#: found is used; the committed images were made with Segoe UI on Windows.
FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def draw_page(lines: list[str], *, size: int) -> Image.Image:
    image = Image.new("RGB", PAGE, (250, 248, 242))
    draw = ImageDraw.Draw(image)
    font = _font(size)
    y = 90
    for line in lines:
        draw.text((90, y), line, fill=(25, 25, 30), font=font)
        y += int(size * 1.55)
    return image


def degrade(image: Image.Image, record: dict, seed: int, line_count: int) -> Image.Image:
    style = record["style"]
    mode = style.get("degrade", "light")
    rng = random.Random(seed)
    if mode == "stamp":
        # Placed from the layout: a stamp across the TOTAL line, a pen scribble through the end of the TAX line and
        # a coffee ring in the corner. Positions are computed from the number of lines, so they follow the page.
        size = 21 if style.get("small") else 26
        step = int(size * 1.55)
        total_y = 90 + step * (line_count - 1)
        tax_y = total_y - step
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        d.text((150, total_y - 52), "PAID", fill=(190, 30, 40, 95), font=_font(120))
        d.rectangle((130, total_y - 62, 470, total_y + 62), outline=(190, 30, 40, 95), width=7)
        d.line([(300, tax_y - 6), (400, tax_y + 20), (330, tax_y + 8), (470, tax_y - 12)], fill=(20, 40, 160, 170), width=4)
        d.ellipse((900, 1250, 1130, 1480), outline=(120, 80, 40, 80), width=14)  # a coffee ring
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    if mode == "photo":
        image = image.resize((int(PAGE[0] * 0.72), int(PAGE[1] * 0.72)), Image.LANCZOS)  # low resolution
        shadow = Image.linear_gradient("L").resize(image.size).rotate(90)
        dark = Image.new("RGB", image.size, (40, 40, 40))
        image = Image.composite(dark, image, shadow.point(lambda v: int(v * 0.45)))
    if style.get("blur"):
        image = image.filter(ImageFilter.GaussianBlur(style["blur"]))
    if style.get("noise"):
        from PIL import ImageChops

        amount = style["noise"]
        count = image.size[0] * image.size[1]
        noise = Image.frombytes("L", image.size, bytes(min(255, max(0, int(rng.gauss(128, amount)))) for _ in range(count)))
        image = Image.blend(image, ImageChops.overlay(image, Image.merge("RGB", (noise, noise, noise))), 0.85)
    if style.get("angle"):
        image = image.rotate(style["angle"], expand=True, resample=Image.BICUBIC, fillcolor=(236, 233, 226))
    return image


def write_image(record: dict, path: Path, seed: int) -> None:
    size = 21 if record["style"].get("small") else 26
    lines = lines_for(record)
    page = draw_page(lines, size=size)
    page = degrade(page, record, seed, len(lines))
    if path.suffix == ".jpg":
        page.save(path, format="JPEG", quality=50)
    else:
        page.save(path, format="PNG")


def write_pdf(record: dict, path: Path) -> None:
    text = "\n".join(lines_for(record))
    pages = text.split("\f")
    path.write_bytes(build_pdf([p.strip("\n") for p in pages]))


def main() -> None:
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    docs = records()
    for index, record in enumerate(docs, start=1):
        target = DATASET_DIR / record["filename"]
        if record["filename"].endswith(".pdf"):
            write_pdf(record, target)
        else:
            write_image(record, target, seed=1000 + index)
    labels = {
        "dataset": DATASET_NAME,
        "synthetic": True,
        "description": (
            "Ten synthetic invoices that are harder to read than invoices_v1: scans and photographs with no text layer, "
            "skew, blur, noise, a stamp and a scribble, other layouts and wording, European formats, a credit note, a "
            "missing due date, a two-page table. Labels are the values used to render each document."
        ),
        "documents": [
            {"document_id": r["document_id"], "filename": r["filename"], "fields": r["fields"]} for r in docs
        ],
    }
    (DATASET_DIR / "labels.json").write_text(json.dumps(labels, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (DATASET_DIR / "FEATURES.json").write_text(
        json.dumps({r["document_id"]: {"kind": r["kind"], "why_hard": r["why_hard"]} for r in docs}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(docs)} documents to {DATASET_DIR}")


if __name__ == "__main__":
    main()
