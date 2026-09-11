"""The extraction service: provider in, persisted extraction out."""

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app import extraction as extraction_service
from app import storage
from app.extraction import ExtractionError, build_field_values, parse_response
from app.extractors import Invoice, UnknownDocumentType
from app.models import Document, DocumentStatus, Extraction, FieldValue
from app.providers import ProviderError, ProviderRefusal

from .conftest import VALID_INVOICE_PAYLOAD, FakeProvider


def _run(engine: Engine, document: Document, provider: FakeProvider) -> uuid.UUID:
    """Extract in its own session and return the extraction id."""
    with Session(engine) as session:
        attached = session.get(Document, document.id)
        assert attached is not None
        result = extraction_service.extract_document(session, attached, provider)
        return result.id


def _load(engine: Engine, extraction_id: uuid.UUID) -> dict[str, object]:
    """Read back everything persisted, detached from the session."""
    with Session(engine) as session:
        extraction = session.get(Extraction, extraction_id)
        assert extraction is not None
        document = session.get(Document, extraction.document_id)
        assert document is not None
        return {
            "extraction": extraction,
            "document": document,
            "fields": {
                value.field_name: value for value in extraction.field_values
            },
            "document_status": document.status,
            "document_error": document.error,
        }


# --- the happy path -------------------------------------------------------


def test_provider_receives_the_pages_schema_and_prompt(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    _run(migrated_engine, rendered_document, fake_provider)

    call = fake_provider.calls[0]
    assert call["schema"] is Invoice
    assert "commercial invoice" in call["prompt"]
    images = call["images"]
    assert [image.page_number for image in images] == [1, 2]
    assert all(image.media_type == "image/png" for image in images)
    assert all(image.data.startswith(b"\x89PNG") for image in images)


def test_extraction_row_records_the_call_metadata(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    extraction = _load(migrated_engine, extraction_id)["extraction"]
    assert extraction.model_name == "fake-model-1"
    assert extraction.prompt_version == "invoice/v1"
    assert extraction.document_id == rendered_document.id
    assert extraction.created_at is not None
    assert extraction.error is None


def test_token_cost_and_latency_are_stored(
    rendered_document: Document, migrated_engine: Engine
) -> None:
    provider = FakeProvider(
        input_tokens=4210, output_tokens=388, cost_usd="0.030650", latency_ms=1234
    )

    extraction_id = _run(migrated_engine, rendered_document, provider)

    extraction = _load(migrated_engine, extraction_id)["extraction"]
    assert extraction.input_tokens == 4210
    assert extraction.output_tokens == 388
    assert extraction.cost_usd == Decimal("0.030650")
    assert extraction.latency_ms == 1234


def test_metadata_is_null_when_the_provider_does_not_supply_it(
    rendered_document: Document, migrated_engine: Engine
) -> None:
    """Unknown cost is stored as unknown, never as zero."""
    provider = FakeProvider(
        input_tokens=None, output_tokens=None, cost_usd=None, latency_ms=None
    )

    extraction_id = _run(migrated_engine, rendered_document, provider)

    extraction = _load(migrated_engine, extraction_id)["extraction"]
    assert extraction.input_tokens is None
    assert extraction.output_tokens is None
    assert extraction.cost_usd is None
    assert extraction.latency_ms is None


def test_raw_response_is_preserved_verbatim(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    """Never discarded: debugging and future evals both need the original."""
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    extraction = _load(migrated_engine, extraction_id)["extraction"]
    assert extraction.raw_response["id"] == "msg_01FakeExtraction"
    assert extraction.raw_response["usage"]["input_tokens"] == 4210
    text = extraction.raw_response["content"][0]["text"]
    assert json.loads(text) == VALID_INVOICE_PAYLOAD


def test_parsed_result_is_stored_as_json(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    parsed = _load(migrated_engine, extraction_id)["extraction"].parsed
    assert parsed is not None
    assert parsed["invoice_number"]["value"] == "INV-2026-0042"
    # Money survives the round trip as an exact decimal string.
    assert parsed["total"]["value"] == "1210.00"


def test_document_becomes_extracted(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    loaded = _load(migrated_engine, extraction_id)
    assert loaded["document_status"] is DocumentStatus.EXTRACTED
    assert loaded["document_error"] is None


# --- field values ---------------------------------------------------------


def test_one_field_value_row_per_schema_field(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    fields = _load(migrated_engine, extraction_id)["fields"]
    assert set(fields) == set(Invoice.model_fields)


def test_field_values_store_the_extracted_text(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    fields = _load(migrated_engine, extraction_id)["fields"]
    assert fields["invoice_number"].value == "INV-2026-0042"
    assert fields["invoice_date"].value == "2026-01-05"
    assert fields["total"].value == "1210.00"
    assert fields["currency"].value == "EUR"


def test_model_reported_confidence_is_stored_as_given(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    fields = _load(migrated_engine, extraction_id)["fields"]
    assert fields["total"].confidence == pytest.approx(0.98)
    assert fields["vendor_address"].confidence == pytest.approx(0.71)
    assert fields["purchase_order_number"].confidence == pytest.approx(0.2)


def test_source_page_is_stored(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    fields = _load(migrated_engine, extraction_id)["fields"]
    assert fields["invoice_number"].source_page == 1
    assert fields["total"].source_page == 2
    assert fields["purchase_order_number"].source_page is None


def test_absent_value_is_stored_as_null(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    fields = _load(migrated_engine, extraction_id)["fields"]
    assert fields["purchase_order_number"].value is None


def test_needs_review_defaults_to_false(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    """Routing to review is milestone 5; nothing sets this yet."""
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    fields = _load(migrated_engine, extraction_id)["fields"]
    assert not any(field.needs_review for field in fields.values())


def test_line_items_are_stored_as_json_text(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    fields = _load(migrated_engine, extraction_id)["fields"]
    rows = json.loads(fields["line_items"].value)
    assert [row["description"] for row in rows] == ["Widget, blue", "Widget, red"]
    assert fields["line_items"].confidence == pytest.approx(0.85)


def test_field_values_cascade_when_the_extraction_goes(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    extraction_id = _run(migrated_engine, rendered_document, fake_provider)

    with Session(migrated_engine) as session:
        session.delete(session.get(Extraction, extraction_id))
        session.commit()
        remaining = session.execute(select(FieldValue)).scalars().all()

    assert remaining == []


def test_build_field_values_rejects_a_schema_without_confidence() -> None:
    """A schema whose fields are bare values cannot report confidence."""
    from pydantic import BaseModel

    class Bare(BaseModel):
        total: str

    with pytest.raises(ExtractionError, match="ExtractedField"):
        list(build_field_values(Bare(total="10")))


# --- failures -------------------------------------------------------------


def test_provider_failure_marks_the_document_failed(
    rendered_document: Document, migrated_engine: Engine
) -> None:
    provider = FakeProvider(raises=ProviderError("Anthropic request failed: boom"))

    extraction_id = _run(migrated_engine, rendered_document, provider)

    loaded = _load(migrated_engine, extraction_id)
    assert loaded["document_status"] is DocumentStatus.FAILED
    assert "boom" in loaded["document_error"]
    assert "boom" in loaded["extraction"].error
    assert loaded["extraction"].parsed is None
    assert loaded["fields"] == {}


def test_refusal_is_recorded_explicitly(
    rendered_document: Document, migrated_engine: Engine
) -> None:
    provider = FakeProvider(raises=ProviderRefusal("declined (category: cyber)"))

    extraction_id = _run(migrated_engine, rendered_document, provider)

    loaded = _load(migrated_engine, extraction_id)
    assert loaded["document_status"] is DocumentStatus.FAILED
    assert "ProviderRefusal" in loaded["extraction"].error
    assert "cyber" in loaded["document_error"]


def test_unparseable_json_keeps_the_raw_response(
    rendered_document: Document, migrated_engine: Engine
) -> None:
    """The broken answer is exactly what you need to fix the prompt."""
    provider = FakeProvider(content="this is not JSON at all")

    extraction_id = _run(migrated_engine, rendered_document, provider)

    loaded = _load(migrated_engine, extraction_id)
    assert loaded["document_status"] is DocumentStatus.FAILED
    assert "not valid JSON" in loaded["extraction"].error
    assert loaded["extraction"].parsed is None
    assert loaded["extraction"].raw_response["content"][0]["text"] == (
        "this is not JSON at all"
    )


def test_schema_violation_fails_loudly(
    rendered_document: Document, migrated_engine: Engine
) -> None:
    payload = {k: v for k, v in VALID_INVOICE_PAYLOAD.items() if k != "total"}
    provider = FakeProvider(payload=payload)

    extraction_id = _run(migrated_engine, rendered_document, provider)

    loaded = _load(migrated_engine, extraction_id)
    assert loaded["document_status"] is DocumentStatus.FAILED
    assert "did not match Invoice" in loaded["extraction"].error
    assert loaded["fields"] == {}


def test_unknown_doc_type_is_refused(
    rendered_document: Document, migrated_engine: Engine, fake_provider: FakeProvider
) -> None:
    with Session(migrated_engine) as session:
        document = session.get(Document, rendered_document.id)
        assert document is not None
        document.doc_type = "bill_of_lading"
        session.commit()

        with pytest.raises(UnknownDocumentType, match="bill_of_lading"):
            extraction_service.extract_document(session, document, fake_provider)


def test_document_without_rendered_pages_is_refused(
    rendered_document: Document,
    migrated_engine: Engine,
    fake_provider: FakeProvider,
    monkeypatch,
) -> None:
    storage.delete_document_files(rendered_document.id)
    monkeypatch.setattr(
        extraction_service,
        "get_provider",
        lambda: pytest.fail("provider must not be built"),
    )

    with Session(migrated_engine) as session:
        document = session.get(Document, rendered_document.id)
        assert document is not None

        with pytest.raises(ExtractionError, match="no rendered pages"):
            extraction_service.extract_document(session, document, fake_provider)

    assert fake_provider.calls == []


def test_background_entry_point_records_failures(
    rendered_document: Document, migrated_engine: Engine, monkeypatch
) -> None:
    """A background task that died quietly would strand the document."""
    storage.delete_document_files(rendered_document.id)

    def no_provider():
        raise AssertionError("a provider must not be built for an unextractable document")

    monkeypatch.setattr(extraction_service, "get_provider", no_provider)

    extraction_service.extract_document_in_background(rendered_document.id)

    with Session(migrated_engine) as session:
        document = session.get(Document, rendered_document.id)
        assert document is not None
        assert document.status is DocumentStatus.FAILED
        assert "no rendered pages" in document.error


# --- parsing helper -------------------------------------------------------


def test_parse_response_accepts_valid_json() -> None:
    parsed = parse_response(json.dumps(VALID_INVOICE_PAYLOAD), Invoice)

    assert parsed.invoice_number.value == "INV-2026-0042"


def test_parse_response_rejects_non_json() -> None:
    with pytest.raises(ExtractionError, match="not valid JSON"):
        parse_response("<html>oops</html>", Invoice)
