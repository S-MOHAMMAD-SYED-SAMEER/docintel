"""PDF and image rendering, exercised against real files."""

import uuid
from pathlib import Path

import pytest
from PIL import Image

from app import rendering, storage
from app.media import SUPPORTED_MEDIA_TYPES
from app.rendering import RenderError

from .conftest import build_pdf, build_png

PDF_MEDIA = next(media for media in SUPPORTED_MEDIA_TYPES if media.is_pdf)
PNG_MEDIA = next(media for media in SUPPORTED_MEDIA_TYPES if media.name == "image/png")


def _store(data: bytes, media) -> tuple[uuid.UUID, Path]:
    document_id = uuid.uuid4()
    stored = storage.save_source(document_id, data, media)
    return document_id, stored.absolute_path


def test_renders_one_png_per_pdf_page(storage_dir: Path) -> None:
    document_id, source = _store(build_pdf(pages=3), PDF_MEDIA)

    pages = rendering.render(document_id, source, PDF_MEDIA)

    assert [page.name for page in pages] == [
        "page-0001.png",
        "page-0002.png",
        "page-0003.png",
    ]
    assert all(page.is_file() for page in pages)
    with Image.open(pages[0]) as image:
        assert image.format == "PNG"


def test_pages_land_in_the_documents_pages_directory(storage_dir: Path) -> None:
    document_id, source = _store(build_pdf(pages=1), PDF_MEDIA)

    pages = rendering.render(document_id, source, PDF_MEDIA)

    assert pages[0].parent == storage.pages_dir(document_id)


def test_render_dpi_is_configurable(
    storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    document_id, source = _store(build_pdf(pages=1), PDF_MEDIA)

    monkeypatch.setenv("DOCINTEL_RENDER_DPI", "72")
    get_settings.cache_clear()
    low = rendering.render(document_id, source, PDF_MEDIA)
    with Image.open(low[0]) as image:
        low_width = image.width

    monkeypatch.setenv("DOCINTEL_RENDER_DPI", "288")
    get_settings.cache_clear()
    high = rendering.render(document_id, source, PDF_MEDIA)
    with Image.open(high[0]) as image:
        high_width = image.width

    get_settings.cache_clear()
    assert high_width > low_width


def test_an_image_becomes_a_single_rgb_page(storage_dir: Path) -> None:
    document_id, source = _store(build_png(), PNG_MEDIA)

    pages = rendering.render(document_id, source, PNG_MEDIA)

    assert len(pages) == 1
    assert pages[0].name == "page-0001.png"
    with Image.open(pages[0]) as image:
        # Alpha is dropped so every page reaches the extractor in one format.
        assert image.mode == "RGB"


def test_rerendering_replaces_stale_pages(storage_dir: Path) -> None:
    document_id, source = _store(build_pdf(pages=3), PDF_MEDIA)
    rendering.render(document_id, source, PDF_MEDIA)

    shorter, _ = _store(build_pdf(pages=1), PDF_MEDIA)
    pages = rendering.render(document_id, storage.resolve(
        f"documents/{shorter}/source.pdf"
    ), PDF_MEDIA)

    assert len(pages) == 1
    assert sorted(p.name for p in storage.pages_dir(document_id).iterdir()) == [
        "page-0001.png"
    ]


def test_corrupt_pdf_raises_render_error(storage_dir: Path) -> None:
    """Valid magic bytes, unreadable content — the renderer must say so."""
    document_id, source = _store(b"%PDF-1.7\nnot actually a pdf", PDF_MEDIA)

    with pytest.raises(RenderError, match="Could not open PDF"):
        rendering.render(document_id, source, PDF_MEDIA)


def test_corrupt_image_raises_render_error(storage_dir: Path) -> None:
    document_id, source = _store(b"\x89PNG\r\n\x1a\ntruncated", PNG_MEDIA)

    with pytest.raises(RenderError, match="Could not read image"):
        rendering.render(document_id, source, PNG_MEDIA)


def test_missing_source_raises_render_error(storage_dir: Path) -> None:
    document_id = uuid.uuid4()

    with pytest.raises(RenderError, match="missing"):
        rendering.render(document_id, storage_dir / "nope.pdf", PDF_MEDIA)
