"""The demo mutation guard: `app/api/demo_guard.py`.

Proves every write-capable route reachable through `demo.app` is either
refused with 403, or -- for the one route deliberately left open,
`POST /api/v1/documents/{id}/extract` -- was never in scope for this guard
because it is the demo's own core, safe interaction (structurally
incapable of reaching a real provider; see `demo/app.py`).
"""

import uuid
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.demo_guard import demo_mutation_refused, require_mutation_allowed
from app.config import get_settings
from app.db.session import reset_engine
from app.main import create_app
from app.models import Correction, Document, FieldValue
from demo.app import create_demo_app
from demo.seed import seed_demo_documents

DATASET_DIR = (
    Path(__file__).resolve().parent.parent / "evals" / "datasets" / "invoices_v1"
)


def _demo_client(migrated_engine: Engine, storage_dir: Path) -> TestClient:
    del storage_dir  # fixture ordering only: points storage at tmp_path first
    return TestClient(create_demo_app())


def _seed(migrated_engine: Engine) -> list[uuid.UUID]:
    with Session(migrated_engine) as session:
        documents = seed_demo_documents(session)
        return [document.id for document in documents]


# --- the guard is a real dependency override, scoped to the demo app -------


def test_the_guard_is_overridden_on_the_demo_app() -> None:
    demo_app = create_demo_app()

    assert require_mutation_allowed in demo_app.dependency_overrides
    assert demo_app.dependency_overrides[require_mutation_allowed] is demo_mutation_refused


def test_calling_the_override_directly_raises_403() -> None:
    try:
        demo_mutation_refused()
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("demo_mutation_refused() did not raise")


def test_the_production_app_does_not_inherit_the_guard_override() -> None:
    create_demo_app()
    production_app = create_app()

    assert require_mutation_allowed not in production_app.dependency_overrides


# --- A: mutation routes refuse in the demo ----------------------------------


def test_demo_upload_is_refused(migrated_engine: Engine, storage_dir: Path) -> None:
    client = _demo_client(migrated_engine, storage_dir)
    data = (DATASET_DIR / "invoice-001.pdf").read_bytes()

    response = client.post(
        "/api/v1/documents",
        files={"file": ("invoice-001.pdf", data, "application/pdf")},
        data={"doc_type": "invoice"},
    )

    assert response.status_code == 403


def test_demo_upload_creates_no_document_row(
    migrated_engine: Engine, storage_dir: Path
) -> None:
    client = _demo_client(migrated_engine, storage_dir)

    with Session(migrated_engine) as session:
        before = session.execute(select(Document.id)).scalars().all()

    client.post(
        "/api/v1/documents",
        files={
            "file": (
                "invoice-001.pdf",
                (DATASET_DIR / "invoice-001.pdf").read_bytes(),
                "application/pdf",
            )
        },
        data={"doc_type": "invoice"},
    )

    with Session(migrated_engine) as session:
        after = session.execute(select(Document.id)).scalars().all()

    assert after == before


def test_demo_json_correction_is_refused_for_a_real_seeded_field(
    migrated_engine: Engine, storage_dir: Path
) -> None:
    client = _demo_client(migrated_engine, storage_dir)
    document_ids = _seed(migrated_engine)

    extract_response = client.post(f"/api/v1/documents/{document_ids[0]}/extract")
    assert extract_response.status_code == 202

    with Session(migrated_engine) as session:
        field_value = session.execute(
            select(FieldValue)
            .join(FieldValue.extraction)
            .where(FieldValue.extraction.has(document_id=document_ids[0]))
            .limit(1)
        ).scalars().first()
        assert field_value is not None, "extraction did not run synchronously"
        field_id = field_value.id

    response = client.post(
        f"/api/v1/review/{field_id}", json={"corrected_value": "tampered"}
    )

    assert response.status_code == 403

    with Session(migrated_engine) as session:
        corrections = session.execute(
            select(Correction).where(Correction.field_value_id == field_id)
        ).scalars().all()
    assert corrections == []


def test_demo_ui_form_correction_is_refused_for_a_real_seeded_field(
    migrated_engine: Engine, storage_dir: Path
) -> None:
    client = _demo_client(migrated_engine, storage_dir)
    document_ids = _seed(migrated_engine)

    extract_response = client.post(f"/api/v1/documents/{document_ids[1]}/extract")
    assert extract_response.status_code == 202

    with Session(migrated_engine) as session:
        field_value = session.execute(
            select(FieldValue)
            .join(FieldValue.extraction)
            .where(FieldValue.extraction.has(document_id=document_ids[1]))
            .limit(1)
        ).scalars().first()
        assert field_value is not None
        field_id = field_value.id

    response = client.post(
        f"/review/{field_id}",
        data={"corrected_value": "tampered"},
        follow_redirects=False,
    )

    assert response.status_code == 403

    with Session(migrated_engine) as session:
        corrections = session.execute(
            select(Correction).where(Correction.field_value_id == field_id)
        ).scalars().all()
    assert corrections == []


# --- extraction and reads remain available in the demo ---------------------


def test_demo_extraction_remains_available(
    migrated_engine: Engine, storage_dir: Path
) -> None:
    client = _demo_client(migrated_engine, storage_dir)
    document_ids = _seed(migrated_engine)

    response = client.post(f"/api/v1/documents/{document_ids[0]}/extract")

    assert response.status_code == 202


def test_demo_reads_remain_available(
    migrated_engine: Engine, storage_dir: Path
) -> None:
    client = _demo_client(migrated_engine, storage_dir)
    _seed(migrated_engine)

    assert client.get("/api/v1/review").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/health").status_code == 200


# --- B: normal/live mode is unaffected --------------------------------------


def test_normal_mode_upload_still_succeeds(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    response = api_client.post(
        "/api/v1/documents",
        files={
            "file": (
                "invoice-001.pdf",
                (DATASET_DIR / "invoice-001.pdf").read_bytes(),
                "application/pdf",
            )
        },
        data={"doc_type": "invoice"},
    )

    assert response.status_code == 201


def test_normal_mode_correction_still_reaches_its_own_404(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    response = api_client.post(
        f"/api/v1/review/{uuid.uuid4()}", json={"corrected_value": "x"}
    )

    assert response.status_code == 404
