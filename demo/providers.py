"""Deterministic, credential-free extraction provider for Demo Mode.

Implements `app.providers.base.ExtractionProvider` exactly -- same
`extract(images, schema, prompt) -> RawExtraction` signature the real
`AnthropicExtractionProvider` implements -- so everything above this seam
(schema parsing, deterministic validation, confidence scoring, persistence,
review, correction, export) runs completely unaware this is not Anthropic.

Matching is by content, never by trust: the SHA-256 of the rendered page
images (what `extract()` actually receives -- there is no access to the
originally uploaded file's own bytes at this seam) is looked up against a
small, committed table of known sample invoices. An unrecognised hash is a
`ProviderError`, exactly like any other extraction failure; there is no
approximate match and no silent empty answer.

Never imports `anthropic`. Never imports a model-loading library. Never
performs network I/O.
"""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.providers.base import PageImage, ProviderError, RawExtraction

MODEL_NAME = "demo-fixture-provider"

FIXTURES_PATH = Path(__file__).resolve().parent / "fixtures" / "invoice_answers.json"


class DemoFixtureError(RuntimeError):
    """A committed demo fixture is missing or malformed.

    Distinct from `ProviderError`: this means the *package* is broken, not
    that a visitor uploaded an unrecognised document. Raised at import time
    so a bad fixture fails loudly before any request can reach it, rather
    than producing a confusing per-request error deep inside extraction.
    """


def _load_fixtures(path: Path = FIXTURES_PATH) -> dict[str, dict[str, Any]]:
    try:
        raw = path.read_text()
    except OSError as exc:
        raise DemoFixtureError(
            f"Demo fixture file is missing or unreadable: {path}"
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DemoFixtureError(f"Demo fixture file is not valid JSON: {path}") from exc

    if not isinstance(data, dict) or not data:
        raise DemoFixtureError(f"Demo fixture file must be a non-empty object: {path}")

    fixtures: dict[str, dict[str, Any]] = {}
    for fixture_id, entry in data.items():
        if fixture_id.startswith("_"):
            # A top-level `_comment`-style key: documentation, not a fixture.
            continue
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("content_sha256"), str)
            or not entry["content_sha256"]
            or not isinstance(entry.get("schema_name"), str)
            or not entry["schema_name"]
            or not isinstance(entry.get("answer"), dict)
        ):
            raise DemoFixtureError(
                f"Demo fixture {fixture_id!r} in {path} is malformed: each "
                "entry needs a non-empty 'content_sha256', a non-empty "
                "'schema_name', and an 'answer' object."
            )
        fixtures[fixture_id] = entry

    if not fixtures:
        raise DemoFixtureError(f"Demo fixture file has no usable entries: {path}")

    return fixtures


def _index_by_hash(
    fixtures: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, dict[str, Any]]]:
    by_hash: dict[str, tuple[str, dict[str, Any]]] = {}
    for fixture_id, entry in fixtures.items():
        content_hash = entry["content_sha256"]
        if content_hash in by_hash:
            raise DemoFixtureError(
                f"Demo fixtures {by_hash[content_hash][0]!r} and "
                f"{fixture_id!r} both declare content_sha256 {content_hash!r} "
                "-- each known sample document must hash uniquely."
            )
        by_hash[content_hash] = (fixture_id, entry)
    return by_hash


# Loaded and validated once, at import time -- a broken fixture file fails
# clearly before any request can reach it.
_FIXTURES = _load_fixtures()
_BY_HASH = _index_by_hash(_FIXTURES)


def hash_pages(images: Sequence[PageImage]) -> str:
    """SHA-256 over the rendered page images, in page order.

    This is what `extract()` actually receives -- rendering happens before
    the provider is ever called, and there is no path back to the originally
    uploaded file's bytes from here. Rendering is deterministic for a given
    document and a given `DOCINTEL_RENDER_DPI`, so the same sample PDF always
    produces the same hash.
    """
    digest = hashlib.sha256()
    for image in sorted(images, key=lambda page: page.page_number):
        digest.update(image.data)
    return digest.hexdigest()


class DemoExtractionProvider:
    """Replays a committed, hand-verified answer for a known sample invoice.

    Every `value` in the committed fixtures is copied verbatim from
    `evals/datasets/invoices_v1/labels.json`, the authoritative ground truth
    used to render those PDFs -- known-correct, not model-inferred. Every
    `confidence` is hand-authored for deterministic demonstration and is
    explicitly NOT a real Anthropic model's self-reported confidence. See
    docs/DEMO.md.
    """

    model_name = MODEL_NAME

    def extract(
        self,
        images: Sequence[PageImage],
        schema: type[BaseModel],
        prompt: str,
    ) -> RawExtraction:
        if not images:
            raise ProviderError("Cannot extract from a document with no pages.")

        content_hash = hash_pages(images)
        match = _BY_HASH.get(content_hash)
        if match is None:
            raise ProviderError(
                "Demo Mode does not recognise this document. It only "
                "answers for its own committed sample invoices "
                "(evals/datasets/invoices_v1/) -- see docs/DEMO.md for the "
                "supported set. This is not a real extraction failure; "
                "upload one of the demo's known samples instead."
            )

        fixture_id, entry = match
        if schema.__name__ != entry["schema_name"]:
            raise ProviderError(
                f"Demo fixture {fixture_id!r} was authored for "
                f"{entry['schema_name']}, but this request asked for "
                f"{schema.__name__} -- the uploaded document's doc_type does "
                "not match the sample it was recognised as."
            )

        answer = entry["answer"]
        content = json.dumps(answer)

        return RawExtraction(
            content=content,
            raw_response={
                "demo_fixture": True,
                "fixture_id": fixture_id,
                "note": (
                    "Deterministic Demo Mode fixture data, not a real "
                    "Anthropic response. Values are copied from the "
                    "committed dataset's ground truth; confidence is "
                    "hand-authored, not model-generated. See docs/DEMO.md."
                ),
                "answer": answer,
            },
            model_name=self.model_name,
            # A stub must not invent a cost or a latency: neither happened.
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            latency_ms=None,
            metadata={
                "demo_fixture": True,
                "fixture_id": fixture_id,
                "source": "evals/datasets/invoices_v1/labels.json (committed, hand-verified)",
            },
        )


__all__ = ["DemoExtractionProvider", "DemoFixtureError", "MODEL_NAME", "hash_pages"]
