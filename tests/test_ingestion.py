"""`app.ingestion.store_upload`: source content identity.

`Document.source_sha256` is the platform-independent identity Demo Mode
fixture matching relies on (see `demo/providers.py`) -- these tests cover
exactly the contract that identity needs to hold, independent of the demo
itself.
"""

import hashlib

from sqlalchemy.orm import Session

from app import ingestion
from app.media import SUPPORTED_MEDIA_TYPES, UnsupportedMediaType
from app.models import Document

PDF_BYTES = b"%PDF-1.7 pretend contents for a source-identity test"


def _pdf_media():
    return next(media for media in SUPPORTED_MEDIA_TYPES if media.is_pdf)


def test_source_sha256_is_the_hash_of_the_exact_uploaded_bytes(
    session: Session, storage_dir
) -> None:
    document = ingestion.store_upload(
        session, filename="invoice.pdf", doc_type="invoice", data=PDF_BYTES
    )

    assert document.source_sha256 == hashlib.sha256(PDF_BYTES).hexdigest()


def test_source_sha256_has_the_expected_hex_digest_format(
    session: Session, storage_dir
) -> None:
    document = ingestion.store_upload(
        session, filename="invoice.pdf", doc_type="invoice", data=PDF_BYTES
    )

    assert document.source_sha256 is not None
    assert len(document.source_sha256) == 64
    assert all(char in "0123456789abcdef" for char in document.source_sha256)


def test_source_sha256_is_stable_for_repeated_uploads_of_the_same_bytes(
    session: Session, storage_dir
) -> None:
    first = ingestion.store_upload(
        session, filename="a.pdf", doc_type="invoice", data=PDF_BYTES
    )
    second = ingestion.store_upload(
        session, filename="b.pdf", doc_type="invoice", data=PDF_BYTES
    )

    assert first.source_sha256 == second.source_sha256


def test_source_sha256_differs_for_different_bytes(
    session: Session, storage_dir
) -> None:
    first = ingestion.store_upload(
        session, filename="a.pdf", doc_type="invoice", data=PDF_BYTES
    )
    second = ingestion.store_upload(
        session, filename="b.pdf", doc_type="invoice", data=PDF_BYTES + b" different"
    )

    assert first.source_sha256 != second.source_sha256


def test_source_sha256_is_computed_from_the_upload_not_the_client_filename(
    session: Session, storage_dir
) -> None:
    """Two different client filenames, identical bytes, must hash the same
    -- identity comes from content, never from what the client called it."""
    first = ingestion.store_upload(
        session, filename="totally-different-name.pdf", doc_type="invoice", data=PDF_BYTES
    )
    second = ingestion.store_upload(
        session, filename="invoice.pdf", doc_type="invoice", data=PDF_BYTES
    )

    assert first.source_sha256 == second.source_sha256


def test_unsupported_upload_creates_no_document_and_no_hash(
    session: Session, storage_dir
) -> None:
    """An upload that fails media-type detection never reaches the point of
    being hashed or persisted -- no misleading partial state."""
    try:
        ingestion.store_upload(
            session, filename="not-a-document.txt", doc_type="invoice", data=b"plain text, not a real document"
        )
    except UnsupportedMediaType:
        pass
    else:
        raise AssertionError("expected UnsupportedMediaType")

    assert session.query(Document).count() == 0
