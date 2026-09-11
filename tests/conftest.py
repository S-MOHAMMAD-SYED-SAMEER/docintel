import io
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pypdfium2 as pdfium
import pytest
import sqlalchemy
from PIL import Image
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import reset_engine
from app.main import create_app

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tests never touch the development database. Override with
# DOCINTEL_TEST_DATABASE_URL to point at a different server.
TEST_DATABASE_URL = os.environ.get(
    "DOCINTEL_TEST_DATABASE_URL",
    "postgresql+psycopg://docintel:docintel@localhost:5432/docintel_test",
)


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give a test a clean settings/engine cache and restore it afterwards."""
    monkeypatch.setenv("DOCINTEL_ENVIRONMENT", "test")
    get_settings.cache_clear()
    reset_engine()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine()


@pytest.fixture
def client(settings_env: None) -> Iterator[TestClient]:
    yield TestClient(create_app())


@pytest.fixture(scope="session")
def database_url() -> str:
    """The test database URL, skipping the test if no server is reachable."""
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect():
            pass
    except sqlalchemy.exc.OperationalError as exc:
        pytest.skip(f"no PostgreSQL at {TEST_DATABASE_URL}: {exc}")
    finally:
        engine.dispose()
    return TEST_DATABASE_URL


@pytest.fixture
def alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(os.path.join(PROJECT_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(PROJECT_ROOT, "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def migrated_engine(
    database_url: str,
    alembic_config: AlembicConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    """A database migrated to head, torn back down to empty afterwards.

    Running the real migration rather than `Base.metadata.create_all` is the
    point: it is the migration, not the model, that has to work on a fresh
    database.
    """
    from alembic import command

    # env.py reads the URL from settings, so the settings must point at the
    # test database for the duration.
    monkeypatch.setenv("DOCINTEL_DATABASE_URL", database_url)
    get_settings.cache_clear()
    reset_engine()

    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(alembic_config, "base")
        get_settings.cache_clear()
        reset_engine()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    with Session(migrated_engine) as db_session:
        yield db_session


@pytest.fixture(autouse=True)
def no_real_anthropic_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard stop on any real Anthropic request.

    Providers are faked everywhere, but this container has ANTHROPIC_* variables
    in its environment, so a mistake would otherwise spend money quietly rather
    than fail a test.
    """
    import anthropic

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "The test suite attempted a real Anthropic API request. "
            "Use FakeProvider, or inject a stub client."
        )

    monkeypatch.setattr(anthropic.Anthropic, "request", blocked, raising=False)
    monkeypatch.setattr(anthropic.Anthropic, "post", blocked, raising=False)


@pytest.fixture
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point storage at a throwaway directory for the duration of a test."""
    root = tmp_path / "storage"
    monkeypatch.setenv("DOCINTEL_STORAGE_DIR", str(root))
    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


@pytest.fixture
def api_client(
    migrated_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A client backed by the migrated test database and temporary storage."""
    monkeypatch.setenv("DOCINTEL_ENVIRONMENT", "test")
    get_settings.cache_clear()
    reset_engine()
    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()
        reset_engine()


def build_pdf(pages: int = 3) -> bytes:
    """A real, minimal PDF — rendering is the thing under test, so no stubs."""
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 260)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def build_pdf_with_text(pages: list[str]) -> bytes:
    """A valid PDF whose pages carry a real, extractable text layer.

    `build_pdf` produces blank pages — useful for rendering, useless for the
    text-layer signal — so this writes the PDF by hand with Helvetica text
    objects rather than stubbing the extractor.
    """

    def escape(text: str) -> str:
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    page_count = len(pages)
    page_ids = [4 + 2 * index for index in range(page_count)]
    content_ids = [5 + 2 * index for index in range(page_count)]

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{pid} 0 R" for pid in page_ids)
            + f"] /Count {page_count} >>"
        ).encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }

    for index, text in enumerate(pages):
        body = ["BT", "/F1 11 Tf", "14 TL", "40 740 Td"]
        body += [f"({escape(line)}) Tj T*" for line in (text.splitlines() or [""])]
        body.append("ET")
        stream = "\n".join(body).encode()
        objects[content_ids[index]] = (
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        objects[page_ids[index]] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 792] "
            "/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_ids[index]} 0 R >>"
        ).encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"

    xref_at = len(out)
    highest = max(objects)
    out += f"xref\n0 {highest + 1}\n".encode() + b"0000000000 65535 f \n"
    for number in range(1, highest + 1):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {highest + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n"
    ).encode() + b"%%EOF\n"
    return bytes(out)


def build_png(size: tuple[int, int] = (120, 80)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (200, 30, 30, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def pdf_bytes() -> bytes:
    return build_pdf()


@pytest.fixture
def png_bytes() -> bytes:
    return build_png()


# --- extraction -----------------------------------------------------------


VALID_INVOICE_PAYLOAD: dict[str, object] = {
    "invoice_number": {"value": "INV-2026-0042", "confidence": 0.97, "source_page": 1},
    "invoice_date": {"value": "2026-01-05", "confidence": 0.94, "source_page": 1},
    "due_date": {"value": "2026-02-04", "confidence": 0.88, "source_page": 1},
    "vendor_name": {"value": "Acme Supplies BV", "confidence": 0.96, "source_page": 1},
    "vendor_address": {
        "value": "12 Kade, Amsterdam",
        "confidence": 0.71,
        "source_page": 1,
    },
    "vendor_tax_id": {"value": "NL123456789B01", "confidence": 0.82, "source_page": 1},
    "customer_name": {"value": "Beta Ltd", "confidence": 0.9, "source_page": 1},
    "purchase_order_number": {"value": None, "confidence": 0.2, "source_page": None},
    "currency": {"value": "EUR", "confidence": 0.99, "source_page": 1},
    "subtotal": {"value": "1000.00", "confidence": 0.93, "source_page": 2},
    "tax": {"value": "210.00", "confidence": 0.91, "source_page": 2},
    "total": {"value": "1210.00", "confidence": 0.98, "source_page": 2},
    "line_items": {
        "value": [
            {
                "description": "Widget, blue",
                "quantity": "10",
                "unit_price": "40.00",
                "amount": "400.00",
            },
            {
                "description": "Widget, red",
                "quantity": "15",
                "unit_price": "40.00",
                "amount": "600.00",
            },
        ],
        "confidence": 0.85,
        "source_page": 2,
    },
}


def anthropic_response(
    text: str,
    *,
    model: str = "claude-opus-5",
    input_tokens: int | None = 4210,
    output_tokens: int | None = 388,
    stop_reason: str = "end_turn",
    stop_details: dict[str, object] | None = None,
) -> dict[str, object]:
    """A response shaped like the Messages API returns."""
    body: dict[str, object] = {
        "id": "msg_01FakeExtraction",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return body


class FakeProvider:
    """An `ExtractionProvider` that answers from a script, with no network.

    Records what it was called with so tests can assert the pipeline handed it
    the right pages, schema and prompt.
    """

    model_name = "fake-model-1"

    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        content: str | None = None,
        raises: Exception | None = None,
        input_tokens: int | None = 4210,
        output_tokens: int | None = 388,
        cost_usd: str | None = "0.030650",
        latency_ms: int | None = 1234,
    ) -> None:
        from decimal import Decimal

        self._content = (
            content
            if content is not None
            else json.dumps(payload if payload is not None else VALID_INVOICE_PAYLOAD)
        )
        self._raises = raises
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._cost_usd = Decimal(cost_usd) if cost_usd is not None else None
        self._latency_ms = latency_ms
        self.calls: list[dict[str, object]] = []

    def extract(self, images, schema, prompt):
        from app.providers import RawExtraction

        self.calls.append({"images": list(images), "schema": schema, "prompt": prompt})
        if self._raises is not None:
            raise self._raises

        return RawExtraction(
            content=self._content,
            raw_response=anthropic_response(
                self._content,
                model=self.model_name,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
            model_name=self.model_name,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cost_usd=self._cost_usd,
            latency_ms=self._latency_ms,
        )


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def rendered_document(api_client: TestClient, migrated_engine: Engine):
    """An uploaded, rendered document ready to extract."""
    from sqlalchemy.orm import Session as OrmSession

    from app.models import Document

    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("acme-invoice.pdf", build_pdf(pages=2), "application/pdf")},
        data={"doc_type": "invoice"},
    )
    assert response.status_code == 201
    document_id = uuid.UUID(response.json()["id"])

    with OrmSession(migrated_engine) as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.page_count == 2
        session.expunge(document)
    return document


@pytest.fixture
def text_layer_document(api_client: TestClient, migrated_engine: Engine):
    """An uploaded, rendered document whose source carries a real text layer."""
    from sqlalchemy.orm import Session as OrmSession

    from app.models import Document

    # Carries every scalar value in VALID_INVOICE_PAYLOAD, so a correct
    # extraction is corroborated by the text layer and a wrong one is not.
    source = build_pdf_with_text(
        [
            "Acme Supplies BV, 12 Kade, Amsterdam\n"
            "VAT NL123456789B01\n"
            "Invoice INV-2026-0042\n"
            "Issued 2026-01-05   Due 2026-02-04\n"
            "Bill to: Beta Ltd     Your order PO-88",
            "Widget, blue   10 x 40.00 = 400.00\n"
            "Widget, red    15 x 40.00 = 600.00\n"
            "Subtotal 1,000.00   Tax 210.00   Total EUR 1,210.00",
        ]
    )
    response = api_client.post(
        "/api/v1/documents",
        files={"file": ("acme-invoice.pdf", source, "application/pdf")},
        data={"doc_type": "invoice"},
    )
    assert response.status_code == 201
    document_id = uuid.UUID(response.json()["id"])

    with OrmSession(migrated_engine) as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.page_count == 2
        session.expunge(document)
    return document
