"""POST /api/v1/documents, end to end against Postgres and real files."""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import storage
from app.models import Document, DocumentStatus

from .conftest import build_pdf


def _reload(engine: Engine, document_id: str) -> Document:
    with Session(engine) as session:
        document = session.get(Document, uuid.UUID(document_id))
        assert document is not None
        session.expunge(document)
        return document


def test_upload_returns_201_and_the_document_id(
    api_client: TestClient, pdf_bytes: bytes
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("acme-invoice.pdf", pdf_bytes, "application/pdf")},
        data={"doc_type": "invoice"},
    )

    assert response.status_code == 201
    body = response.json()
    uuid.UUID(body["id"])
    assert body["filename"] == "acme-invoice.pdf"
    assert body["doc_type"] == "invoice"
    # Rendering happens after the response, so the client sees the pre-render state.
    assert body["status"] == DocumentStatus.UPLOADED
    assert body["page_count"] is None
    assert body["error"] is None


def test_upload_creates_the_document_row(
    api_client: TestClient, migrated_engine: Engine, pdf_bytes: bytes
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("acme-invoice.pdf", pdf_bytes, "application/pdf")},
        data={"doc_type": "invoice"},
    )

    document = _reload(migrated_engine, response.json()["id"])
    assert document.filename == "acme-invoice.pdf"
    assert document.doc_type == "invoice"
    assert document.uploaded_at is not None


def test_upload_stores_the_source_file(
    api_client: TestClient, migrated_engine: Engine, storage_dir: Path, pdf_bytes: bytes
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("acme-invoice.pdf", pdf_bytes, "application/pdf")},
        data={"doc_type": "invoice"},
    )

    document = _reload(migrated_engine, response.json()["id"])
    source = storage.resolve(document.storage_path)
    assert source.is_file()
    assert source.read_bytes() == pdf_bytes
    assert storage_dir in source.parents


def test_rendering_populates_page_count(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("four-pager.pdf", build_pdf(pages=4), "application/pdf")},
        data={"doc_type": "invoice"},
    )

    document = _reload(migrated_engine, response.json()["id"])
    assert document.page_count == 4
    assert document.error is None


def test_rendering_writes_one_page_image_per_page(
    api_client: TestClient, storage_dir: Path
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("two-pager.pdf", build_pdf(pages=2), "application/pdf")},
        data={"doc_type": "invoice"},
    )

    pages = storage.pages_dir(uuid.UUID(response.json()["id"]))
    assert sorted(page.name for page in pages.iterdir()) == [
        "page-0001.png",
        "page-0002.png",
    ]


def test_successful_render_leaves_the_document_processing(
    api_client: TestClient, migrated_engine: Engine, pdf_bytes: bytes
) -> None:
    """Pages exist but nothing is extracted yet — that arrives in milestone 4."""
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("acme-invoice.pdf", pdf_bytes, "application/pdf")},
        data={"doc_type": "invoice"},
    )

    document = _reload(migrated_engine, response.json()["id"])
    assert document.status is DocumentStatus.PROCESSING


def test_image_upload_counts_as_one_page(
    api_client: TestClient, migrated_engine: Engine, png_bytes: bytes
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("receipt.png", png_bytes, "image/png")},
        data={"doc_type": "receipt"},
    )

    assert response.status_code == 201
    document = _reload(migrated_engine, response.json()["id"])
    assert document.page_count == 1
    assert document.status is DocumentStatus.PROCESSING


def test_doc_type_is_normalised(
    api_client: TestClient, migrated_engine: Engine, pdf_bytes: bytes
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("x.pdf", pdf_bytes, "application/pdf")},
        data={"doc_type": "  Purchase_Order  "},
    )

    assert response.json()["doc_type"] == "purchase_order"


def test_client_filename_cannot_escape_the_storage_root(
    api_client: TestClient, migrated_engine: Engine, storage_dir: Path, pdf_bytes: bytes
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("../../etc/passwd.pdf", pdf_bytes, "application/pdf")},
        data={"doc_type": "invoice"},
    )

    document = _reload(migrated_engine, response.json()["id"])
    assert document.filename == "passwd.pdf"
    assert storage_dir in storage.resolve(document.storage_path).parents


# --- failure cases -------------------------------------------------------


def test_plain_text_upload_is_rejected(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("notes.txt", b"just some notes", "text/plain")},
        data={"doc_type": "invoice"},
    )

    assert response.status_code == 415
    assert "Unsupported file type" in response.json()["detail"]


def test_text_disguised_as_a_pdf_is_rejected(
    api_client: TestClient, storage_dir: Path
) -> None:
    """The declared content type is a claim; the bytes decide."""
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("invoice.pdf", b"still just text", "application/pdf")},
        data={"doc_type": "invoice"},
    )

    assert response.status_code == 415
    # Nothing was written for a file we refused.
    assert not (storage_dir / "documents").exists()


def test_empty_upload_is_rejected(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("empty.pdf", b"", "application/pdf")},
        data={"doc_type": "invoice"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Uploaded file is empty."


def test_upload_over_the_size_limit_is_rejected(
    api_client: TestClient, monkeypatch, storage_dir: Path
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DOCINTEL_MAX_UPLOAD_BYTES", "1024")
    get_settings.cache_clear()
    try:
        response = api_client.post(
            "/api/v1/documents",
            files={"file": ("big.pdf", b"%PDF-1.7" + b"\x00" * 4096, "application/pdf")},
            data={"doc_type": "invoice"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 413
    assert not (storage_dir / "documents").exists()


def test_missing_doc_type_is_rejected(
    api_client: TestClient, pdf_bytes: bytes
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("x.pdf", pdf_bytes, "application/pdf")},
    )

    assert response.status_code == 422


def test_malformed_doc_type_is_rejected(
    api_client: TestClient, pdf_bytes: bytes
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("x.pdf", pdf_bytes, "application/pdf")},
        data={"doc_type": "not a valid type!"},
    )

    assert response.status_code == 422
    assert "doc_type must be" in response.json()["detail"]


def test_corrupt_pdf_is_stored_then_marked_failed(
    api_client: TestClient, migrated_engine: Engine, storage_dir: Path
) -> None:
    """A file we accepted but cannot render fails loudly, with the reason kept."""
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("broken.pdf", b"%PDF-1.7\nnot a pdf", "application/pdf")},
        data={"doc_type": "invoice"},
    )

    assert response.status_code == 201
    document = _reload(migrated_engine, response.json()["id"])
    assert document.status is DocumentStatus.FAILED
    assert document.error is not None
    assert "Could not open PDF" in document.error
    assert document.page_count is None
    # The source is kept so the failure can be investigated.
    assert storage.resolve(document.storage_path).is_file()
