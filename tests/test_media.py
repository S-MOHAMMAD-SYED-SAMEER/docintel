"""File-type detection, which runs before anything touches the disk."""

import pytest

from app import media
from app.media import UnsupportedMediaType

from .conftest import build_pdf, build_png


def test_detects_a_pdf() -> None:
    detected = media.detect(build_pdf(pages=1))
    assert detected.name == "application/pdf"
    assert detected.extension == ".pdf"
    assert detected.is_pdf


def test_detects_a_png() -> None:
    detected = media.detect(build_png())
    assert detected.name == "image/png"
    assert not detected.is_pdf


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 16),
        ("tiff-le", b"II*\x00" + b"\x00" * 16),
        ("tiff-be", b"MM\x00*" + b"\x00" * 16),
        ("webp", b"RIFF\x24\x00\x00\x00WEBPVP8 "),
    ],
)
def test_detects_other_supported_images(name: str, data: bytes) -> None:
    assert media.detect(data).name.startswith("image/")


def test_rejects_plain_text() -> None:
    with pytest.raises(UnsupportedMediaType):
        media.detect(b"this is not a document at all")


def test_rejects_empty_input() -> None:
    with pytest.raises(UnsupportedMediaType, match="empty"):
        media.detect(b"")


def test_riff_that_is_not_webp_is_rejected() -> None:
    """A RIFF container holding WAVE audio must not pass as an image."""
    with pytest.raises(UnsupportedMediaType):
        media.detect(b"RIFF\x24\x00\x00\x00WAVEfmt ")
