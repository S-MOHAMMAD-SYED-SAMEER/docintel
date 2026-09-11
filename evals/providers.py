"""Providers the harness can run against.

`anthropic` is the real one and the only one whose numbers mean anything.
`stub` exists so the harness itself can be exercised — in CI, or before
spending credits — without a network call. It answers every document the same
way and never sees the labels, so its accuracy is genuinely poor; that is the
point, not a defect.
"""

import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from app.providers import ExtractionProvider, PageImage, RawExtraction, get_provider

ANTHROPIC = "anthropic"
STUB = "stub"
PROVIDER_CHOICES = (ANTHROPIC, STUB)

STUB_MODEL_NAME = "stub-provider"
STUB_WARNING = (
    "Ran against the stub provider: these numbers measure the harness, "
    "not extraction quality. Use --provider anthropic for a real evaluation."
)

# What the stub says about any one field: nothing, with low confidence.
STUB_FIELD: dict[str, Any] = {"value": None, "confidence": 0.1, "source_page": None}


def stub_answer(schema: type[BaseModel]) -> dict[str, Any]:
    """An empty but schema-valid answer for whatever type it is handed.

    Derived from the schema, never from the labels, and it invents no value —
    so it is document-type agnostic and its accuracy is genuinely near zero.
    """
    return {
        field_name: dict(STUB_FIELD)
        for field_name in getattr(schema, "model_fields", {})
    }


class StubProvider:
    """Answers offline, identically, every time."""

    model_name = STUB_MODEL_NAME

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self._answer = answer

    def extract(
        self,
        images: Sequence[PageImage],
        schema: type[BaseModel],
        prompt: str,
    ) -> RawExtraction:
        content = json.dumps(
            self._answer if self._answer is not None else stub_answer(schema)
        )
        return RawExtraction(
            content=content,
            raw_response={
                "id": "stub",
                "model": self.model_name,
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": None, "output_tokens": None},
            },
            model_name=self.model_name,
            # No usage and no timing: a stub must not invent a cost or a
            # latency, and the report says "not available" instead.
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            latency_ms=None,
        )


def build(choice: str) -> ExtractionProvider:
    if choice == STUB:
        return StubProvider()
    if choice == ANTHROPIC:
        return get_provider()
    raise ValueError(
        f"Unknown provider {choice!r}. Choose from: {', '.join(PROVIDER_CHOICES)}."
    )
