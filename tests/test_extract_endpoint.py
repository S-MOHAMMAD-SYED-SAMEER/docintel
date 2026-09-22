"""POST /api/v1/documents/{id}/extract — the minimal trigger for extraction."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import Document, DocumentStatus, Extraction
from app.providers import get_provider

from .conftest import FakeProvider


@pytest.fixture
def provider_in_use(api_client: TestClient) -> FakeProvider:
    """Replace the configured provider so no request ever leaves the process.

    Overrides the FastAPI dependency the `/extract` route resolves its
    provider through (`Depends(get_provider)`), the same seam
    `demo/app.py` overrides for Demo Mode -- not a monkeypatch of
    `app.extraction`'s own module-level reference, which the route no
    longer calls directly.
    """
    provider = FakeProvider()
    api_client.app.dependency_overrides[get_provider] = lambda: provider
    return provider


def test_extract_returns_202(
    api_client: TestClient, rendered_document: Document, provider_in_use: FakeProvider
) -> None:
    response = api_client.post(f"/api/v1/documents/{rendered_document.id}/extract")

    assert response.status_code == 202
    body = response.json()
    assert body["document_id"] == str(rendered_document.id)
    assert body["doc_type"] == "invoice"
    assert body["page_count"] == 2


def test_extract_persists_an_extraction(
    api_client: TestClient,
    rendered_document: Document,
    migrated_engine: Engine,
    provider_in_use: FakeProvider,
) -> None:
    api_client.post(f"/api/v1/documents/{rendered_document.id}/extract")

    with Session(migrated_engine) as session:
        extraction = session.execute(select(Extraction)).scalar_one()
        assert extraction.document_id == rendered_document.id
        assert extraction.prompt_version == "invoice/v1"
        assert len(extraction.field_values) == 13

        document = session.get(Document, rendered_document.id)
        assert document is not None
        # Scored on the way in; the fixture has fields below the threshold.
        assert document.status is DocumentStatus.NEEDS_REVIEW

    assert len(provider_in_use.calls) == 1


def test_unknown_document_is_404(
    api_client: TestClient, provider_in_use: FakeProvider
) -> None:
    response = api_client.post(f"/api/v1/documents/{uuid.uuid4()}/extract")

    assert response.status_code == 404


def test_document_without_pages_is_409(
    api_client: TestClient,
    migrated_engine: Engine,
    provider_in_use: FakeProvider,
) -> None:
    """Extraction needs rendered pages; asking early is a conflict, not a crash."""
    with Session(migrated_engine) as session:
        document = Document(
            filename="not-rendered.pdf",
            storage_path="documents/x/source.pdf",
            doc_type="invoice",
        )
        session.add(document)
        session.commit()
        document_id = document.id

    response = api_client.post(f"/api/v1/documents/{document_id}/extract")

    assert response.status_code == 409
    assert "rendered pages" in response.json()["detail"]
    assert provider_in_use.calls == []


def test_unknown_doc_type_is_422(
    api_client: TestClient,
    rendered_document: Document,
    migrated_engine: Engine,
    provider_in_use: FakeProvider,
) -> None:
    with Session(migrated_engine) as session:
        document = session.get(Document, rendered_document.id)
        assert document is not None
        document.doc_type = "bill_of_lading"
        session.commit()

    response = api_client.post(f"/api/v1/documents/{rendered_document.id}/extract")

    assert response.status_code == 422
    assert "bill_of_lading" in response.json()["detail"]
    assert provider_in_use.calls == []


def test_upload_does_not_trigger_extraction(
    api_client: TestClient,
    rendered_document: Document,
    migrated_engine: Engine,
    provider_in_use: FakeProvider,
) -> None:
    """Milestone 3's upload flow is unchanged — extraction is opt-in."""
    with Session(migrated_engine) as session:
        assert session.execute(select(Extraction)).all() == []

    assert provider_in_use.calls == []
