"""The human correction workflow."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app import corrections
from app.models import Correction, Document, DocumentStatus, FieldValue

from .test_review_api import CONFIDENT_PAYLOAD, MANY_FLAGGED_PAYLOAD, _payload_with


def _queue(client: TestClient) -> list[dict]:
    return client.get("/api/v1/review").json()["items"]


def _field(client: TestClient, name: str) -> dict:
    return next(item for item in _queue(client) if item["field_name"] == name)


def _correct(client: TestClient, field_id: str, value: str | None):
    return client.post(f"/api/v1/review/{field_id}", json={"corrected_value": value})


def _reload(engine: Engine, field_id: str) -> FieldValue:
    with Session(engine) as session:
        field_value = session.get(FieldValue, uuid.UUID(field_id))
        assert field_value is not None
        session.refresh(field_value, ["corrections"])
        session.expunge_all()
        return field_value


def _status(engine: Engine, document_id: uuid.UUID) -> DocumentStatus:
    with Session(engine) as session:
        return session.get(Document, document_id).status


# --- the correction record ------------------------------------------------


def test_correction_creates_a_corrections_row(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field = _field(api_client, "total")

    response = _correct(api_client, field["field_id"], "1210.00")

    assert response.status_code == 201
    with Session(migrated_engine) as session:
        row = session.execute(select(Correction)).scalar_one()
        assert str(row.field_value_id) == field["field_id"]


def test_the_original_value_is_preserved(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    """The model's answer is never silently discarded."""
    extracted_document(_payload_with(total="1500.00"))
    field = _field(api_client, "total")

    _correct(api_client, field["field_id"], "1210.00")

    with Session(migrated_engine) as session:
        row = session.execute(select(Correction)).scalar_one()
        assert row.original_value == "1500.00"
        assert row.corrected_value == "1210.00"


def test_corrected_at_is_populated(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))

    body = _correct(api_client, _field(api_client, "total")["field_id"], "1210.00")

    assert body.json()["corrected_at"]
    with Session(migrated_engine) as session:
        assert session.execute(select(Correction)).scalar_one().corrected_at


def test_the_field_now_holds_the_corrected_value(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field_id = _field(api_client, "total")["field_id"]

    _correct(api_client, field_id, "1210.00")

    stored = _reload(migrated_engine, field_id)
    assert stored.value == "1210.00"
    assert stored.is_corrected is True


def test_a_correction_can_clear_a_hallucinated_value(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)
    field_id = _field(api_client, "vendor_address")["field_id"]

    response = _correct(api_client, field_id, None)

    assert response.status_code == 201
    stored = _reload(migrated_engine, field_id)
    assert stored.value is None
    assert stored.is_corrected is True


# --- auditability ---------------------------------------------------------


def test_the_field_no_longer_needs_review(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field_id = _field(api_client, "total")["field_id"]

    _correct(api_client, field_id, "1210.00")

    assert _reload(migrated_engine, field_id).needs_review is False
    assert "total" not in {item["field_name"] for item in _queue(api_client)}


def test_model_confidence_is_unchanged_by_a_correction(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field = _field(api_client, "total")

    _correct(api_client, field["field_id"], "1210.00")

    stored = _reload(migrated_engine, field["field_id"])
    assert stored.model_confidence == pytest.approx(field["model_confidence"])


def test_the_original_score_and_validation_survive(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    """They describe the model's answer and stay true of it."""
    extracted_document(_payload_with(total="1500.00"))
    field = _field(api_client, "total")

    _correct(api_client, field["field_id"], "1210.00")

    stored = _reload(migrated_engine, field["field_id"])
    assert stored.confidence == pytest.approx(field["confidence"])
    assert (
        "totals.subtotal_plus_tax_equals_total"
        in stored.validation["blocking_failures"]
    )
    assert stored.validation["model_confidence"] == pytest.approx(
        field["model_confidence"]
    )


def test_the_raw_model_response_survives(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field_id = _field(api_client, "total")["field_id"]

    _correct(api_client, field_id, "1210.00")

    with Session(migrated_engine) as session:
        field_value = session.get(FieldValue, uuid.UUID(field_id))
        raw = field_value.extraction.raw_response
        assert raw["id"] == "msg_01FakeExtraction"
        assert field_value.extraction.parsed["total"]["value"] == "1500.00"


def test_repeated_corrections_chain_back_to_the_original(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field_id = _field(api_client, "total")["field_id"]

    _correct(api_client, field_id, "1210.00")
    second = _correct(api_client, field_id, "1210.50")

    assert second.status_code == 201
    with Session(migrated_engine) as session:
        chain = corrections.history(session, uuid.UUID(field_id))

    assert [(row.original_value, row.corrected_value) for row in chain] == [
        ("1500.00", "1210.00"),
        ("1210.00", "1210.50"),
    ]
    assert _reload(migrated_engine, field_id).value == "1210.50"


def test_correcting_an_already_reviewed_field_is_allowed(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    """A reviewer may fix a field the scorer was happy with."""
    extracted_document(CONFIDENT_PAYLOAD)
    with Session(migrated_engine) as session:
        field_value = (
            session.execute(select(FieldValue).filter_by(field_name="vendor_name"))
            .scalar_one()
        )
        field_id = str(field_value.id)
        assert field_value.needs_review is False

    response = _correct(api_client, field_id, "Acme Supplies B.V.")

    assert response.status_code == 201
    assert _reload(migrated_engine, field_id).value == "Acme Supplies B.V."


def test_an_unknown_field_is_a_404(api_client: TestClient, migrated_engine: Engine) -> None:
    response = _correct(api_client, str(uuid.uuid4()), "x")

    assert response.status_code == 404


def test_a_malformed_field_id_is_rejected(
    api_client: TestClient, migrated_engine: Engine
) -> None:
    assert _correct(api_client, "not-a-uuid", "x").status_code == 422


def test_a_missing_corrected_value_is_rejected(
    api_client: TestClient, extracted_document
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field_id = _field(api_client, "total")["field_id"]

    response = api_client.post(f"/api/v1/review/{field_id}", json={})

    assert response.status_code == 422


# --- document status ------------------------------------------------------


def test_document_stays_needs_review_while_fields_remain(
    api_client: TestClient, extracted_document, text_layer_document: Document,
    migrated_engine: Engine
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)
    queue = _queue(api_client)
    assert len(queue) > 1

    body = _correct(api_client, queue[0]["field_id"], "corrected").json()

    assert body["document_status"] == DocumentStatus.NEEDS_REVIEW
    assert body["outstanding_review_fields"] == len(queue) - 1
    assert _status(migrated_engine, text_layer_document.id) is (
        DocumentStatus.NEEDS_REVIEW
    )


def test_document_becomes_reviewed_once_nothing_is_outstanding(
    api_client: TestClient, extracted_document, text_layer_document: Document,
    migrated_engine: Engine
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)

    while queue := _queue(api_client):
        body = _correct(api_client, queue[0]["field_id"], "corrected").json()

    assert body["document_status"] == DocumentStatus.REVIEWED
    assert body["outstanding_review_fields"] == 0
    assert _status(migrated_engine, text_layer_document.id) is DocumentStatus.REVIEWED


def test_the_last_correction_is_what_flips_the_document(
    api_client: TestClient, extracted_document, text_layer_document: Document,
    migrated_engine: Engine
) -> None:
    extracted_document(MANY_FLAGGED_PAYLOAD)
    queue = _queue(api_client)

    for item in queue[:-1]:
        _correct(api_client, item["field_id"], "corrected")
        assert _status(migrated_engine, text_layer_document.id) is (
            DocumentStatus.NEEDS_REVIEW
        )

    _correct(api_client, queue[-1]["field_id"], "corrected")
    assert _status(migrated_engine, text_layer_document.id) is DocumentStatus.REVIEWED


def test_a_correction_does_not_call_the_model(
    api_client: TestClient, extracted_document, monkeypatch
) -> None:
    """A human correction is ground truth; nothing is re-extracted."""
    from app import extraction as extraction_service

    extracted_document(_payload_with(total="1500.00"))
    field_id = _field(api_client, "total")["field_id"]
    monkeypatch.setattr(
        extraction_service,
        "get_provider",
        lambda: pytest.fail("a correction must not reach the provider"),
    )

    assert _correct(api_client, field_id, "1210.00").status_code == 201


def test_deleting_a_field_takes_its_corrections_with_it(
    api_client: TestClient, extracted_document, migrated_engine: Engine
) -> None:
    extracted_document(_payload_with(total="1500.00"))
    field_id = _field(api_client, "total")["field_id"]
    _correct(api_client, field_id, "1210.00")

    with Session(migrated_engine) as session:
        session.delete(session.get(FieldValue, uuid.UUID(field_id)))
        session.commit()
        assert session.execute(select(Correction)).scalars().all() == []
