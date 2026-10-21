"""The page-text-layer signal: reading it, and matching against it."""

import uuid
from pathlib import Path

from app import rendering, storage
from app.media import SUPPORTED_MEDIA_TYPES
from app.validation.text_layer import TextLayer, normalise

from .conftest import build_pdf, build_pdf_with_text, build_png

PDF_MEDIA = next(media for media in SUPPORTED_MEDIA_TYPES if media.is_pdf)
PNG_MEDIA = next(media for media in SUPPORTED_MEDIA_TYPES if media.name == "image/png")

INVOICE_TEXT = [
    "Acme Supplies BV      Invoice INV-2026-0042",
    "Subtotal 1,000.00   Tax 210.00   Total EUR 1,210.00",
]


def _write(storage_dir: Path, data: bytes, media=PDF_MEDIA) -> Path:
    return storage.save_source(uuid.uuid4(), data, media).absolute_path


# --- extraction -----------------------------------------------------------


def test_reads_the_text_layer_of_a_born_digital_pdf(storage_dir: Path) -> None:
    source = _write(storage_dir, build_pdf_with_text(INVOICE_TEXT))

    pages = rendering.extract_text_layer(source, PDF_MEDIA)

    assert len(pages) == 2
    assert "INV-2026-0042" in pages[0]
    assert "1,210.00" in pages[1]


def test_a_pdf_without_text_reports_no_text_layer(storage_dir: Path) -> None:
    """A scan renders fine and says nothing — that is absence, not failure."""
    source = _write(storage_dir, build_pdf(pages=2))

    assert rendering.extract_text_layer(source, PDF_MEDIA) == []


def test_an_image_has_no_text_layer(storage_dir: Path) -> None:
    source = _write(storage_dir, build_png(), PNG_MEDIA)

    assert rendering.extract_text_layer(source, PNG_MEDIA) == []


def test_a_missing_file_has_no_text_layer(storage_dir: Path) -> None:
    assert rendering.extract_text_layer(storage_dir / "gone.pdf", PDF_MEDIA) == []


def test_an_unreadable_pdf_has_no_text_layer(storage_dir: Path) -> None:
    source = _write(storage_dir, b"%PDF-1.7\nnot really")

    assert rendering.extract_text_layer(source, PDF_MEDIA) == []


# --- matching -------------------------------------------------------------


def _layer() -> TextLayer:
    return TextLayer.from_pages(INVOICE_TEXT)


def test_exists_distinguishes_absence_from_emptiness() -> None:
    assert _layer().exists
    assert not TextLayer().exists
    assert not TextLayer.from_pages([]).exists


def test_finds_an_exact_string() -> None:
    assert _layer().contains("INV-2026-0042")


def test_finds_a_value_across_pages() -> None:
    assert _layer().contains("Acme Supplies BV")
    assert _layer().contains("210.00")


def test_ignores_thousands_separators_and_symbols() -> None:
    """"1210.00" and "1,210.00" are the same number."""
    assert _layer().contains("1210.00")
    assert TextLayer.from_pages(["Total: EUR 1.210,00"]).contains("1210.00")
    assert TextLayer.from_pages(["Total: \u20ac1,210.00"]).contains("1210.00")


def test_ignores_case_and_spacing() -> None:
    assert _layer().contains("acme   supplies bv")


def test_does_not_find_an_absent_value() -> None:
    assert not _layer().contains("INV-9999-0001")
    assert not _layer().contains("4321.00")


def test_a_value_with_no_comparable_characters_is_not_found() -> None:
    assert not _layer().contains("---")
    assert not _layer().contains("")


def test_nothing_is_found_without_a_text_layer() -> None:
    assert not TextLayer().contains("INV-2026-0042")


def test_normalise_strips_presentation() -> None:
    assert normalise("EUR 1,210.00") == "eur 121000"
    assert normalise("  Acme   Supplies  ") == "acme supplies"
    assert normalise("Acme\u200b Supplies") == "acme supplies"


# --- the three real misses of the 2026-10-21 runs --------------------------
# The printed VAT number was FR40123456824. The model returned these three values
# on different invoices; the old substring check found all three.

VAT_PAGE = ["Acme Supplies BV   VAT FR40123456824   Invoice INV-2026-0042"]


def _vat_layer() -> TextLayer:
    return TextLayer.from_pages(VAT_PAGE)


def test_the_printed_vat_number_is_found() -> None:
    assert _vat_layer().contains("FR40123456824")


def test_a_dropped_last_digit_is_not_found() -> None:
    assert not _vat_layer().contains("FR4012345682")


def test_a_dropped_middle_digit_is_not_found() -> None:
    assert not _vat_layer().contains("FR4012345824")


def test_a_zero_width_space_in_the_value_is_not_found() -> None:
    assert not _vat_layer().contains("FR4012345\u200b6824")


def test_a_space_inside_the_value_is_not_found() -> None:
    assert not _vat_layer().contains("FR4012345682 4")


def test_a_zero_width_space_in_the_page_is_ignored() -> None:
    layer = TextLayer.from_pages(["VAT FR401234\u200b56824"])

    assert layer.contains("FR40123456824")


def test_other_invisible_characters_in_the_value_are_not_found() -> None:
    for char in ("\u00ad", "\u200c", "\u200d", "\u2060", "\ufeff", "\u200e"):
        assert not _vat_layer().contains(f"FR4012345{char}6824"), repr(char)


def test_a_substring_of_a_token_is_not_found() -> None:
    layer = TextLayer.from_pages(["Total 1,210.00"])

    assert not layer.contains("210.00")
    assert not layer.contains("1,21")


def test_a_run_of_whole_tokens_is_found_in_order_only() -> None:
    layer = TextLayer.from_pages(["Goethestrasse 14,\n80336 Munchen"])

    assert layer.contains("Goethestrasse 14, 80336 Munchen")
    assert not layer.contains("14 Goethestrasse")


def test_an_altered_spelling_is_not_found() -> None:
    assert not TextLayer.from_pages(["80336 Munchen"]).contains("80336 M\u00fcnchen")


def test_compatibility_forms_are_normalised() -> None:
    assert TextLayer.from_pages(["Total \uff11\uff12\uff13"]).contains("123")
    assert TextLayer.from_pages(["A\u00a0B"]).contains("A B")
