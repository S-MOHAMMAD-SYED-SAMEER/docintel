"""DemoExtractionProvider: deterministic, credential-free, in isolation.

Mirrors the shape of `tests/test_extraction.py`'s own provider-level tests,
but exercises `demo.providers.DemoExtractionProvider` directly rather than
through the full pipeline -- see `test_demo_pipeline.py` for the end-to-end
case using the real `extract_document`.
"""

import io
import json
from decimal import Decimal
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from app.config import get_settings
from app.extractors.invoice import Invoice
from app.extractors.purchase_order import PurchaseOrder
from app.providers.base import PageImage, ProviderError
from demo.providers import DemoExtractionProvider, DemoFixtureError

DATASET_DIR = (
    Path(__file__).resolve().parent.parent / "evals" / "datasets" / "invoices_v1"
)

PDF_BASE_DPI = 72


def _render_to_page_images(source: Path | bytes) -> list[PageImage]:
    """The same render math `app/rendering.py::_render_pdf` uses, producing
    real `PageImage` objects for a real PDF -- so these tests exercise the
    provider against exactly the bytes it receives in production, without
    going through the full upload/API pipeline."""
    scale = get_settings().render_dpi / PDF_BASE_DPI
    pdf = pdfium.PdfDocument(source if isinstance(source, Path) else io.BytesIO(source))
    try:
        images: list[PageImage] = []
        for index in range(len(pdf)):
            bitmap = pdf[index].render(scale=scale)
            buffer = io.BytesIO()
            bitmap.to_pil().save(buffer, format="PNG")
            images.append(
                PageImage(
                    page_number=index + 1,
                    media_type="image/png",
                    data=buffer.getvalue(),
                )
            )
        return images
    finally:
        pdf.close()


def _blank_pdf_bytes(pages: int = 1) -> bytes:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 260)
    buffer = io.BytesIO()
    pdf.save(buffer)
    pdf.close()
    return buffer.getvalue()


@pytest.fixture
def provider() -> DemoExtractionProvider:
    return DemoExtractionProvider()


# --- known fixture ----------------------------------------------------------


def test_known_sample_invoice_produces_the_committed_answer(
    provider: DemoExtractionProvider,
) -> None:
    images = _render_to_page_images(DATASET_DIR / "invoice-001.pdf")

    raw = provider.extract(images, Invoice, "irrelevant prompt text")

    parsed = Invoice.model_validate(json.loads(raw.content))
    assert parsed.invoice_number.value == "INV-2026-1001"
    assert parsed.total.value == Decimal("145.20")
    assert len(parsed.line_items.value) == 2
    assert raw.raw_response["demo_fixture"] is True
    assert raw.raw_response["fixture_id"] == "invoice-001"


def test_same_fixture_extracted_twice_is_byte_identical(
    provider: DemoExtractionProvider,
) -> None:
    images = _render_to_page_images(DATASET_DIR / "invoice-004.pdf")

    first = provider.extract(images, Invoice, "prompt")
    second = provider.extract(images, Invoice, "a completely different prompt")

    assert first.content == second.content
    assert first.raw_response == second.raw_response


def test_no_token_cost_or_latency_is_fabricated(
    provider: DemoExtractionProvider,
) -> None:
    images = _render_to_page_images(DATASET_DIR / "invoice-001.pdf")

    raw = provider.extract(images, Invoice, "prompt")

    assert raw.input_tokens is None
    assert raw.output_tokens is None
    assert raw.cost_usd is None
    assert raw.latency_ms is None


def test_the_review_worthy_fixture_field_is_below_the_default_threshold(
    provider: DemoExtractionProvider,
) -> None:
    """invoice-003's purchase_order_number is deliberately authored below
    the default confidence threshold, to demonstrate the review queue
    honestly through the real scoring logic -- not to fabricate the review
    state itself. This proves only the fixture's authored input; the real
    scoring/routing decision is proven end to end in test_demo_pipeline.py.
    """
    images = _render_to_page_images(DATASET_DIR / "invoice-003.pdf")

    raw = provider.extract(images, Invoice, "prompt")
    parsed = Invoice.model_validate(json.loads(raw.content))

    assert parsed.purchase_order_number.value is None
    assert parsed.purchase_order_number.confidence < get_settings().confidence_threshold


# --- unknown / mismatched documents ------------------------------------------


def test_unknown_document_raises_provider_error(
    provider: DemoExtractionProvider,
) -> None:
    unknown_images = _render_to_page_images(_blank_pdf_bytes())

    with pytest.raises(ProviderError):
        provider.extract(unknown_images, Invoice, "prompt")


def test_no_pages_raises_provider_error(provider: DemoExtractionProvider) -> None:
    with pytest.raises(ProviderError):
        provider.extract([], Invoice, "prompt")


def test_known_document_with_mismatched_schema_raises_provider_error(
    provider: DemoExtractionProvider,
) -> None:
    """A known sample's pages, requested against the wrong document type's
    schema (e.g. uploaded as doc_type=purchase_order), must fail clearly --
    not silently return an invoice-shaped answer that later fails a
    confusing JSON-parse error deep inside extraction."""
    images = _render_to_page_images(DATASET_DIR / "invoice-001.pdf")

    with pytest.raises(ProviderError):
        provider.extract(images, PurchaseOrder, "prompt")


# --- fixture loading / malformed fixtures ------------------------------------


def test_malformed_fixture_file_fails_at_load_time(tmp_path: Path) -> None:
    import demo.providers as providers_module

    broken = tmp_path / "broken.json"
    broken.write_text("{not valid json")

    with pytest.raises(DemoFixtureError):
        providers_module._load_fixtures(broken)


def test_fixture_missing_required_keys_fails_at_load_time(tmp_path: Path) -> None:
    import demo.providers as providers_module

    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"invoice-999": {"answer": {}}}))

    with pytest.raises(DemoFixtureError):
        providers_module._load_fixtures(broken)


def test_duplicate_content_hash_fails_at_index_time() -> None:
    import demo.providers as providers_module

    fixtures = {
        "a": {"content_sha256": "same", "schema_name": "Invoice", "answer": {}},
        "b": {"content_sha256": "same", "schema_name": "Invoice", "answer": {}},
    }

    with pytest.raises(DemoFixtureError):
        providers_module._index_by_hash(fixtures)


# --- no external dependency --------------------------------------------------


def test_the_demo_provider_module_never_imports_a_real_or_network_provider() -> None:
    import demo.providers as providers_module

    source = Path(providers_module.__file__).read_text()
    assert "import anthropic" not in source
    assert "sentence_transformers" not in source
    assert "torch" not in source
    assert "import requests" not in source
    assert "import httpx" not in source
