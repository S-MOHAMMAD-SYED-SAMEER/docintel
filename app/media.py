"""What we accept, and how we recognise it.

A declared content type is a claim by the client, and a file extension is a
claim by whoever named the file. Neither is checked, so both are verified
against the leading bytes of the upload before anything is written to disk.
"""

from dataclasses import dataclass

PDF = "application/pdf"


@dataclass(frozen=True)
class MediaType:
    name: str
    extension: str
    # Byte prefixes that identify the format. A file must match one of them.
    signatures: tuple[bytes, ...]

    @property
    def is_pdf(self) -> bool:
        return self.name == PDF


SUPPORTED_MEDIA_TYPES: tuple[MediaType, ...] = (
    MediaType(PDF, ".pdf", (b"%PDF-",)),
    MediaType("image/png", ".png", (b"\x89PNG\r\n\x1a\n",)),
    MediaType("image/jpeg", ".jpg", (b"\xff\xd8\xff",)),
    MediaType("image/tiff", ".tif", (b"II*\x00", b"MM\x00*")),
    # WEBP is "RIFF" + 4 size bytes + "WEBP", so it is matched separately below.
    MediaType("image/webp", ".webp", (b"RIFF",)),
)

SUPPORTED_MEDIA_TYPE_NAMES = tuple(media.name for media in SUPPORTED_MEDIA_TYPES)

# Longest signature we need to inspect.
SNIFF_BYTES = 12


class UnsupportedMediaType(Exception):
    """The upload is not a PDF or a supported image."""


def _matches(media: MediaType, data: bytes) -> bool:
    if media.name == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return any(data.startswith(signature) for signature in media.signatures)


def detect(data: bytes) -> MediaType:
    """Identify the upload from its own bytes.

    The client's declared content type is deliberately not consulted: a
    mislabelled .pdf that is really text must be rejected here rather than
    failing later in the renderer.
    """
    if not data:
        raise UnsupportedMediaType("Uploaded file is empty.")

    for media in SUPPORTED_MEDIA_TYPES:
        if _matches(media, data):
            return media

    raise UnsupportedMediaType(
        "Unsupported file type. Accepted: " + ", ".join(SUPPORTED_MEDIA_TYPE_NAMES) + "."
    )
