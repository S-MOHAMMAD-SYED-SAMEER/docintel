"""Deterministic, credential-free extraction provider for Demo Mode.

Implements `app.providers.base.ExtractionProvider` exactly -- same
`extract(images, schema, prompt) -> RawExtraction` signature the real
`AnthropicExtractionProvider` implements -- so everything above this seam
(schema parsing, deterministic validation, confidence scoring, persistence,
review, correction, export) runs completely unaware this is not Anthropic.

Identity -- which known sample document this is -- is decided *before*
`extract()` is ever called, by `for_source_sha256()`, keyed on
`Document.source_sha256`: the SHA-256 of the originally uploaded PDF bytes,
computed once at ingestion, before any rendering. This is deliberately not a
hash of the rendered page images `extract()` receives -- a rendered image's
bytes depend on the rasterizer and PNG encoder that produced them, which are
not guaranteed byte-identical across operating systems or library versions;
the uploaded source bytes are. See `demo/generate_fixture_identity.py` for
how the committed fixture hashes are produced.

Never imports `anthropic`. Never imports a model-loading library. Never
performs network I/O.
"""

import json
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
            or not isinstance(entry.get("source_sha256"), str)
            or not entry["source_sha256"]
            or not isinstance(entry.get("schema_name"), str)
            or not entry["schema_name"]
            or not isinstance(entry.get("answer"), dict)
        ):
            raise DemoFixtureError(
                f"Demo fixture {fixture_id!r} in {path} is malformed: each "
                "entry needs a non-empty 'source_sha256', a non-empty "
                "'schema_name', and an 'answer' object."
            )
        fixtures[fixture_id] = entry

    if not fixtures:
        raise DemoFixtureError(f"Demo fixture file has no usable entries: {path}")

    return fixtures


def _index_by_source_hash(
    fixtures: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, dict[str, Any]]]:
    by_hash: dict[str, tuple[str, dict[str, Any]]] = {}
    for fixture_id, entry in fixtures.items():
        source_hash = entry["source_sha256"]
        if source_hash in by_hash:
            raise DemoFixtureError(
                f"Demo fixtures {by_hash[source_hash][0]!r} and "
                f"{fixture_id!r} both declare source_sha256 {source_hash!r} "
                "-- each known sample document must hash uniquely."
            )
        by_hash[source_hash] = (fixture_id, entry)
    return by_hash


# Loaded and validated once, at import time -- a broken fixture file fails
# clearly before any request can reach it.
_FIXTURES = _load_fixtures()
_BY_SOURCE_HASH = _index_by_source_hash(_FIXTURES)


class DemoExtractionProvider:
    """Replays a committed, hand-verified answer for a known sample invoice.

    Every `value` in the committed fixtures is copied verbatim from
    `evals/datasets/invoices_v1/labels.json`, the authoritative ground truth
    used to render those PDFs -- known-correct, not model-inferred. Every
    `confidence` is hand-authored for deterministic demonstration and is
    explicitly NOT a real Anthropic model's self-reported confidence. See
    docs/DEMO.md.

    Bound to a specific fixture match (or the lack of one) at construction
    time, via `for_source_sha256()` -- not re-derived from `extract()`'s own
    `images` argument, which is exactly the platform-dependent value this
    design avoids keying identity on.
    """

    model_name = MODEL_NAME

    def __init__(
        self, fixture_id: str | None, entry: dict[str, Any] | None
    ) -> None:
        self._fixture_id = fixture_id
        self._entry = entry

    @classmethod
    def for_source_sha256(
        cls, source_sha256: str | None
    ) -> "DemoExtractionProvider":
        """The provider instance for a document with this source identity.

        `source_sha256` is `None` for a document with no computed hash (for
        example, a row written before this column existed) -- treated
        exactly the same as an unrecognised hash, never as a wildcard
        match. Matching a fixture is deferred to construction time, not
        done here eagerly as an error: whether this resolves to a known
        fixture or not, the failure (if any) surfaces from `extract()`
        later, at the same point it always has.
        """
        match = _BY_SOURCE_HASH.get(source_sha256) if source_sha256 else None
        if match is None:
            return cls(None, None)
        fixture_id, entry = match
        return cls(fixture_id, entry)

    def extract(
        self,
        images: list[PageImage],
        schema: type[BaseModel],
        prompt: str,
    ) -> RawExtraction:
        if not images:
            raise ProviderError("Cannot extract from a document with no pages.")

        if self._entry is None:
            raise ProviderError(
                "Demo Mode does not recognise this document. It only "
                "answers for its own committed sample invoices "
                "(evals/datasets/invoices_v1/) -- see docs/DEMO.md for the "
                "supported set. This is not a real extraction failure; "
                "upload one of the demo's known samples instead."
            )

        fixture_id, entry = self._fixture_id, self._entry
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


__all__ = ["DemoExtractionProvider", "DemoFixtureError", "MODEL_NAME"]
