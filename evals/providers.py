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

# A fixed, plausible answer. It is not derived from any label.
STUB_ANSWER: dict[str, Any] = {
    "invoice_number": {"value": "INV-0000-0000", "confidence": 0.5, "source_page": 1},
    "invoice_date": {"value": "2026-01-01", "confidence": 0.5, "source_page": 1},
    "due_date": {"value": "2026-01-15", "confidence": 0.5, "source_page": 1},
    "vendor_name": {"value": "Unknown Vendor", "confidence": 0.5, "source_page": 1},
    "vendor_address": {"value": None, "confidence": 0.1, "source_page": None},
    "vendor_tax_id": {"value": None, "confidence": 0.1, "source_page": None},
    "customer_name": {"value": None, "confidence": 0.1, "source_page": None},
    "purchase_order_number": {"value": None, "confidence": 0.1, "source_page": None},
    "currency": {"value": "EUR", "confidence": 0.5, "source_page": 1},
    "subtotal": {"value": "100.00", "confidence": 0.5, "source_page": 2},
    "tax": {"value": "21.00", "confidence": 0.5, "source_page": 2},
    "total": {"value": "121.00", "confidence": 0.5, "source_page": 2},
    "line_items": {"value": [], "confidence": 0.3, "source_page": 2},
}


class StubProvider:
    """Answers offline, identically, every time."""

    model_name = STUB_MODEL_NAME

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self._content = json.dumps(answer if answer is not None else STUB_ANSWER)

    def extract(
        self,
        images: Sequence[PageImage],
        schema: type[BaseModel],
        prompt: str,
    ) -> RawExtraction:
        return RawExtraction(
            content=self._content,
            raw_response={
                "id": "stub",
                "model": self.model_name,
                "content": [{"type": "text", "text": self._content}],
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
