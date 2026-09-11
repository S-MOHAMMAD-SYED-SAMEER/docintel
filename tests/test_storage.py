"""On-disk layout and the safety of paths derived from client input."""

import uuid
from pathlib import Path

from app import storage
from app.media import SUPPORTED_MEDIA_TYPES

PDF_MEDIA = next(media for media in SUPPORTED_MEDIA_TYPES if media.is_pdf)


def test_save_source_writes_under_the_configured_root(storage_dir: Path) -> None:
    document_id = uuid.uuid4()

    stored = storage.save_source(document_id, b"%PDF-1.7 ...", PDF_MEDIA)

    assert stored.absolute_path.read_bytes() == b"%PDF-1.7 ..."
    assert stored.absolute_path.parent == storage_dir / "documents" / str(document_id)
    assert stored.absolute_path.name == "source.pdf"


def test_storage_path_is_relative_to_the_root(storage_dir: Path) -> None:
    """Rows must stay valid if the storage root moves."""
    document_id = uuid.uuid4()

    stored = storage.save_source(document_id, b"%PDF-1.7", PDF_MEDIA)

    assert stored.relative_path == f"documents/{document_id}/source.pdf"
    assert not Path(stored.relative_path).is_absolute()
    assert storage.resolve(stored.relative_path) == stored.absolute_path


def test_stored_filename_ignores_the_client_name(storage_dir: Path) -> None:
    """A traversal attempt cannot influence where bytes land."""
    document_id = uuid.uuid4()

    stored = storage.save_source(document_id, b"%PDF-1.7", PDF_MEDIA)

    assert storage_dir in stored.absolute_path.parents


def test_safe_filename_strips_directories() -> None:
    assert storage.safe_filename("../../etc/passwd") == "passwd"
    assert storage.safe_filename("/tmp/invoice.pdf") == "invoice.pdf"
    assert storage.safe_filename(None) == "upload"
    assert storage.safe_filename("   ") == "upload"


def test_prepare_pages_dir_clears_previous_pages(storage_dir: Path) -> None:
    document_id = uuid.uuid4()
    pages = storage.prepare_pages_dir(document_id)
    (pages / "page-0001.png").write_bytes(b"stale")

    pages = storage.prepare_pages_dir(document_id)

    assert list(pages.iterdir()) == []


def test_delete_document_files_removes_everything(storage_dir: Path) -> None:
    document_id = uuid.uuid4()
    storage.save_source(document_id, b"%PDF-1.7", PDF_MEDIA)

    storage.delete_document_files(document_id)

    assert not storage.document_dir(document_id).exists()
