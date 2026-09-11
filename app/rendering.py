"""Page rendering: a stored document becomes one PNG per page.

Every document type ends up in the same shape — `pages/page-NNNN.png` — so the
extractor that arrives in milestone 4 never has to care whether the upload was
a PDF or a photo.
"""

import logging
import uuid
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from app import storage
from app.config import get_settings
from app.media import MediaType

logger = logging.getLogger(__name__)

PAGE_FILENAME = "page-{number:04d}.png"
# pypdfium2 renders at 72 dpi by default; scale is relative to that.
PDF_BASE_DPI = 72


class RenderError(Exception):
    """The stored file could not be turned into page images."""


def page_path(pages: Path, number: int) -> Path:
    return pages / PAGE_FILENAME.format(number=number)


def render(document_id: uuid.UUID, source: Path, media: MediaType) -> list[Path]:
    """Render `source` into the document's pages directory.

    Returns the page images in order. Raises `RenderError` on anything the
    renderer cannot make sense of — the caller is expected to record that
    failure against the document rather than swallow it.
    """
    if not source.is_file():
        raise RenderError(f"Stored file is missing: {source}")

    pages = storage.prepare_pages_dir(document_id)
    if media.is_pdf:
        return _render_pdf(source, pages)
    return [_render_image(source, pages)]


def _render_pdf(source: Path, pages: Path) -> list[Path]:
    scale = get_settings().render_dpi / PDF_BASE_DPI

    try:
        pdf = pdfium.PdfDocument(source)
    except pdfium.PdfiumError as exc:
        raise RenderError(f"Could not open PDF: {exc}") from exc

    try:
        if len(pdf) == 0:
            raise RenderError("PDF contains no pages.")

        rendered: list[Path] = []
        for index in range(len(pdf)):
            try:
                bitmap = pdf[index].render(scale=scale)
                image = bitmap.to_pil()
            except pdfium.PdfiumError as exc:
                raise RenderError(f"Could not render page {index + 1}: {exc}") from exc

            destination = page_path(pages, index + 1)
            image.save(destination, format="PNG")
            rendered.append(destination)

        return rendered
    finally:
        pdf.close()


def _render_image(source: Path, pages: Path) -> Path:
    """Normalise a single uploaded image to the same PNG page format."""
    try:
        with Image.open(source) as image:
            # Drop alpha and exotic modes so every page is a plain RGB PNG.
            image.convert("RGB").save(page_path(pages, 1), format="PNG")
    except (UnidentifiedImageError, OSError) as exc:
        raise RenderError(f"Could not read image: {exc}") from exc

    return page_path(pages, 1)
