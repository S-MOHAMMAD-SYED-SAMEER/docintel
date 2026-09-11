"""Where uploaded documents and their rendered pages live on disk.

Layout, relative to `DOCINTEL_STORAGE_DIR`:

    documents/<document_id>/source.pdf
    documents/<document_id>/pages/page-0001.png

`Document.storage_path` holds the *relative* path, so the storage root can be
moved or mounted elsewhere without rewriting every row.

The stored filename is derived from the detected media type, never from the
client-supplied name, so a filename like `../../etc/passwd` cannot escape the
storage root. The original name is kept in the `filename` column instead.
"""

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.media import MediaType

DOCUMENTS_DIR = "documents"
PAGES_DIR = "pages"
SOURCE_STEM = "source"


@dataclass(frozen=True)
class StoredSource:
    relative_path: str
    absolute_path: Path


def storage_root() -> Path:
    return get_settings().storage_dir


def document_dir(document_id: uuid.UUID) -> Path:
    return storage_root() / DOCUMENTS_DIR / str(document_id)


def pages_dir(document_id: uuid.UUID) -> Path:
    return document_dir(document_id) / PAGES_DIR


def resolve(relative_path: str) -> Path:
    """Absolute path for a value taken from `Document.storage_path`."""
    return storage_root() / relative_path


def safe_filename(filename: str | None) -> str:
    """The client's filename, reduced to a bare basename for display only."""
    candidate = Path(filename or "").name.strip()
    return candidate[:512] if candidate else "upload"


def save_source(document_id: uuid.UUID, data: bytes, media: MediaType) -> StoredSource:
    """Write the uploaded bytes and return their path relative to the root."""
    directory = document_dir(document_id)
    directory.mkdir(parents=True, exist_ok=True)

    absolute = directory / f"{SOURCE_STEM}{media.extension}"
    absolute.write_bytes(data)

    relative = absolute.relative_to(storage_root())
    return StoredSource(relative_path=str(relative), absolute_path=absolute)


def prepare_pages_dir(document_id: uuid.UUID) -> Path:
    """An empty pages directory, so a re-render never mixes old and new pages."""
    directory = pages_dir(document_id)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory


def delete_document_files(document_id: uuid.UUID) -> None:
    """Remove everything stored for a document. Used to undo a failed upload."""
    shutil.rmtree(document_dir(document_id), ignore_errors=True)
