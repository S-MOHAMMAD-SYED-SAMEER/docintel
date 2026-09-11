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
    assert normalise("EUR 1,210.00") == "eur121000"
    assert normalise("  Acme Supplies  ") == "acmesupplies"
