"""DemoExtractionProvider: deterministic, credential-free, in isolation.

Mirrors the shape of `tests/test_extraction.py`'s own provider-level tests,
but exercises `demo.providers.DemoExtractionProvider` directly rather than
through the full pipeline -- see `test_demo_pipeline.py` for the end-to-end
case using the real `extract_document`.

Fixture identity is the source PDF's own SHA-256 (`for_source_sha256`), the
same value `app.ingestion.store_upload` computes and persists on every
document -- never a hash of the rendered page images. `_render_to_page_images`
still produces real `PageImage`s, because `extract()` still needs pages to
work with; it just no longer decides *which* fixture answers for them.
"""

import hashlib
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


def _source_sha256(source: Path | bytes) -> str:
    """A source PDF's own content identity -- computed exactly the way
    `app.ingestion.store_upload` computes it: SHA-256 of the raw file bytes,
    before any rendering."""
    data = source.read_bytes() if isinstance(source, Path) else source
    return hashlib.sha256(data).hexdigest()


def _render_to_page_images(
    source: Path | bytes, *, scale: float | None = None
) -> list[PageImage]:
    """Real rendered `PageImage`s for a real PDF -- what `extract()` needs to
    do its actual work, even though fixture identity no longer depends on
    these bytes. `scale` lets a test deliberately render at a different
    resolution than the application default, to prove identity doesn't care.
    """
    if scale is None:
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


def _provider_for(pdf_name: str) -> DemoExtractionProvider:
    """The provider a document with this sample PDF's identity would get --
    exactly what `demo/app.py`'s per-request dependency constructs, given a
    document whose `source_sha256` is this PDF's hash."""
    return DemoExtractionProvider.for_source_sha256(
        _source_sha256(DATASET_DIR / pdf_name)
    )


# --- known fixture ------------------------------------------------------------


def test_known_sample_invoice_produces_the_committed_answer() -> None:
    provider = _provider_for("invoice-001.pdf")
    images = _render_to_page_images(DATASET_DIR / "invoice-001.pdf")

    raw = provider.extract(images, Invoice, "irrelevant prompt text")

    parsed = Invoice.model_validate(json.loads(raw.content))
    assert parsed.invoice_number.value == "INV-2026-1001"
    assert parsed.total.value == Decimal("145.20")
    assert len(parsed.line_items.value) == 2
    assert raw.raw_response["demo_fixture"] is True
    assert raw.raw_response["fixture_id"] == "invoice-001"


def test_same_fixture_extracted_twice_is_byte_identical() -> None:
    provider = _provider_for("invoice-004.pdf")
    images = _render_to_page_images(DATASET_DIR / "invoice-004.pdf")

    first = provider.extract(images, Invoice, "prompt")
    second = provider.extract(images, Invoice, "a completely different prompt")

    assert first.content == second.content
    assert first.raw_response == second.raw_response


def test_fixture_identity_is_independent_of_rendering_output() -> None:
    """The same source PDF, rendered at two deliberately different
    resolutions -- producing genuinely different PNG bytes -- must still
    match the exact same fixture: identity comes from the uploaded PDF's own
    bytes, never from what a renderer happened to produce from them. This is
    the property that makes the demo's fixture matching portable across
    operating systems and rendering-library versions."""
    provider = _provider_for("invoice-001.pdf")
    images_default = _render_to_page_images(DATASET_DIR / "invoice-001.pdf")
    images_other_scale = _render_to_page_images(
        DATASET_DIR / "invoice-001.pdf", scale=1.0
    )

    # The two renderings must actually differ, or this test would prove
    # nothing about identity being independent of rendering.
    assert images_default[0].data != images_other_scale[0].data

    first = provider.extract(images_default, Invoice, "prompt")
    second = provider.extract(images_other_scale, Invoice, "prompt")

    assert first.raw_response["fixture_id"] == "invoice-001"
    assert second.raw_response["fixture_id"] == "invoice-001"
    assert first.content == second.content


def test_no_token_cost_or_latency_is_fabricated() -> None:
    provider = _provider_for("invoice-001.pdf")
    images = _render_to_page_images(DATASET_DIR / "invoice-001.pdf")

    raw = provider.extract(images, Invoice, "prompt")

    assert raw.input_tokens is None
    assert raw.output_tokens is None
    assert raw.cost_usd is None
    assert raw.latency_ms is None


def test_the_review_worthy_fixture_field_is_below_the_default_threshold() -> None:
    """invoice-003's purchase_order_number is deliberately authored below
    the default confidence threshold, to demonstrate the review queue
    honestly through the real scoring logic -- not to fabricate the review
    state itself. This proves only the fixture's authored input; the real
    scoring/routing decision is proven end to end in test_demo_pipeline.py.
    """
    provider = _provider_for("invoice-003.pdf")
    images = _render_to_page_images(DATASET_DIR / "invoice-003.pdf")

    raw = provider.extract(images, Invoice, "prompt")
    parsed = Invoice.model_validate(json.loads(raw.content))

    assert parsed.purchase_order_number.value is None
    assert parsed.purchase_order_number.confidence < get_settings().confidence_threshold


# --- unknown / mismatched documents --------------------------------------------


def test_unknown_document_raises_provider_error() -> None:
    """A source PDF with no committed fixture at all must be refused, never
    guessed at -- content-based matching, not a fallback."""
    unknown_bytes = _blank_pdf_bytes()
    provider = DemoExtractionProvider.for_source_sha256(_source_sha256(unknown_bytes))
    unknown_images = _render_to_page_images(unknown_bytes)

    with pytest.raises(ProviderError):
        provider.extract(unknown_images, Invoice, "prompt")


def test_no_source_identity_raises_provider_error() -> None:
    """A document with no computed `source_sha256` (for example, a row from
    before this column existed) is treated as unrecognised, never as a
    wildcard match."""
    provider = DemoExtractionProvider.for_source_sha256(None)
    images = _render_to_page_images(DATASET_DIR / "invoice-001.pdf")

    with pytest.raises(ProviderError):
        provider.extract(images, Invoice, "prompt")


def test_no_pages_raises_provider_error() -> None:
    provider = _provider_for("invoice-001.pdf")

    with pytest.raises(ProviderError):
        provider.extract([], Invoice, "prompt")


def test_known_document_with_mismatched_schema_raises_provider_error() -> None:
    """A known sample's pages, requested against the wrong document type's
    schema (e.g. uploaded as doc_type=purchase_order), must fail clearly --
    not silently return an invoice-shaped answer that later fails a
    confusing JSON-parse error deep inside extraction."""
    provider = _provider_for("invoice-001.pdf")
    images = _render_to_page_images(DATASET_DIR / "invoice-001.pdf")

    with pytest.raises(ProviderError):
        provider.extract(images, PurchaseOrder, "prompt")


# --- fixture loading / malformed fixtures --------------------------------------


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


def test_duplicate_source_hash_fails_at_index_time() -> None:
    import demo.providers as providers_module

    fixtures = {
        "a": {"source_sha256": "same", "schema_name": "Invoice", "answer": {}},
        "b": {"source_sha256": "same", "schema_name": "Invoice", "answer": {}},
    }

    with pytest.raises(DemoFixtureError):
        providers_module._index_by_source_hash(fixtures)


# --- no external dependency ----------------------------------------------------


def test_the_demo_provider_module_never_imports_a_real_or_network_provider() -> None:
    import demo.providers as providers_module

    source = Path(providers_module.__file__).read_text()
    assert "import anthropic" not in source
    assert "sentence_transformers" not in source
    assert "torch" not in source
    assert "import requests" not in source
    assert "import httpx" not in source
