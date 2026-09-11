"""Anthropic vision implementation of `ExtractionProvider`.

This is the only module in the project that imports the Anthropic SDK. It
converts page images into a Messages request, asks for the extractor's schema
via structured outputs, and returns the response verbatim alongside its cost.
"""

import base64
import json
import logging
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import anthropic
from pydantic import BaseModel

from app.config import get_settings
from app.providers.base import (
    PageImage,
    ProviderError,
    ProviderRefusal,
    RawExtraction,
)

logger = logging.getLogger(__name__)

# USD per million tokens, from Anthropic's published pricing. A model missing
# from this table yields cost_usd = None rather than a guessed number.
PRICING_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}

TOKENS_PER_MTOK = Decimal(1_000_000)


def estimate_cost_usd(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    """Cost of one call, or None when the model's pricing is unknown."""
    pricing = PRICING_USD_PER_MTOK.get(model)
    if pricing is None or input_tokens is None or output_tokens is None:
        return None

    input_rate, output_rate = pricing
    cost = (
        Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
    ) / TOKENS_PER_MTOK
    # Six decimal places: a single page costs fractions of a cent.
    return cost.quantize(Decimal("0.000001"))


def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's JSON schema, tightened for structured outputs.

    Structured outputs expects every object to be closed and to list all of its
    properties as required. Pydantic leaves fields with defaults out of
    `required`, so they are added back — a nullable field is still answered,
    just with null.
    """
    document = schema.model_json_schema()
    _close_objects(document)
    return document


def _close_objects(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
        for value in node.values():
            _close_objects(value)
    elif isinstance(node, list):
        for item in node:
            _close_objects(item)


class AnthropicExtractionProvider:
    """Calls Claude with the rendered pages and the extractor's schema."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        settings = get_settings()
        self._model = model or settings.extraction_model
        self._max_tokens = max_tokens or settings.extraction_max_tokens
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key or None
        )

    @property
    def model_name(self) -> str:
        return self._model

    def extract(
        self,
        images: Sequence[PageImage],
        schema: type[BaseModel],
        prompt: str,
    ) -> RawExtraction:
        if not images:
            raise ProviderError("Cannot extract from a document with no pages.")

        started = time.perf_counter()
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": self._content(images, prompt)}],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": json_schema_for(schema),
                    }
                },
            )
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        return self._to_raw_extraction(response, latency_ms)

    def _content(
        self, images: Sequence[PageImage], prompt: str
    ) -> list[dict[str, Any]]:
        """Pages first, in order, then the instruction."""
        content: list[dict[str, Any]] = []
        for image in sorted(images, key=lambda page: page.page_number):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": base64.standard_b64encode(image.data).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": prompt})
        return content

    def _to_raw_extraction(self, response: Any, latency_ms: int) -> RawExtraction:
        raw_response = _as_dict(response)

        stop_reason = raw_response.get("stop_reason")
        if stop_reason == "refusal":
            details = raw_response.get("stop_details") or {}
            raise ProviderRefusal(
                "The model declined to extract this document "
                f"(category: {details.get('category')})."
            )

        text = "".join(
            block.get("text", "")
            for block in raw_response.get("content", [])
            if block.get("type") == "text"
        )
        if not text.strip():
            raise ProviderError(
                f"Model returned no text content (stop_reason: {stop_reason})."
            )
        if stop_reason == "max_tokens":
            raise ProviderError(
                "Model response was cut off by max_tokens before the schema was "
                "complete; raise DOCINTEL_EXTRACTION_MAX_TOKENS."
            )

        usage = raw_response.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        model_name = raw_response.get("model") or self._model

        return RawExtraction(
            content=text,
            raw_response=raw_response,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost_usd(model_name, input_tokens, output_tokens),
            latency_ms=latency_ms,
            metadata={"stop_reason": stop_reason},
        )


def _as_dict(response: Any) -> dict[str, Any]:
    """The SDK response as a plain JSON-safe dict, for storing verbatim."""
    if isinstance(response, dict):
        return response
    for method in ("to_dict", "model_dump"):
        converter = getattr(response, method, None)
        if callable(converter):
            return json.loads(json.dumps(converter(), default=str))
    raise ProviderError(f"Cannot serialise provider response of type {type(response)}.")
