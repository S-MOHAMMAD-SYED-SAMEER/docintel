"""Anthropic vision implementation of `ExtractionProvider`.

This is the only module in the project that imports the Anthropic SDK. It
converts page images into a Messages request, asks for the extractor's schema
via structured outputs, and returns the response verbatim alongside its cost.
"""

import base64
import json
import logging
import re
import time
from collections.abc import Sequence
from typing import Any

import anthropic
from pydantic import BaseModel

from app.config import get_settings
from app.providers.base import (
    PageImage,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderConnectionError,
    ProviderError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderRefusal,
    ProviderTimeout,
    ProviderUnavailable,
    RawExtraction,
)
from app.providers.pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

#: Keywords the structured-outputs endpoint does not accept (numeric and string constraints, and defaults),
#: removed from the schema before it is sent. The limits they express are enforced afterwards, in Python:
#: Pydantic's own validation (`ge`/`le` on the confidence and the page number) runs on every answer in
#: `app.extraction.parse_response`, so a confidence of 1.4 or a page 0 still fails the extraction.
UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "default",
    }
)


def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's JSON schema, tightened for structured outputs.

    Structured outputs expects every object to be closed and to list all of its
    properties as required. Pydantic leaves fields with defaults out of
    `required`, so they are added back: a nullable field is still answered,
    just with null. Keywords the endpoint rejects (`UNSUPPORTED_SCHEMA_KEYWORDS`) are dropped; the
    constraint they stated is still checked on the way back in.
    """
    document = schema.model_json_schema()
    _close_objects(document)
    _strip_unsupported(document)
    return document


def _strip_unsupported(node: Any) -> None:
    if isinstance(node, dict):
        for keyword in UNSUPPORTED_SCHEMA_KEYWORDS & set(node):
            del node[keyword]
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                # The keys here are property NAMES (a field could be called "default"); only the schemas
                # under them are walked.
                for sub in value.values():
                    _strip_unsupported(sub)
            else:
                _strip_unsupported(value)
    elif isinstance(node, list):
        for item in node:
            _strip_unsupported(item)


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


def classify_api_error(exc: anthropic.APIError) -> ProviderError:
    """Turn the SDK's error into the provider error that says what to do about it.

    The message never contains the request or a key; it names the failure and, where the service gave
    one, its own description.
    """
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeout(f"Anthropic did not answer in time: {exc}")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderConnectionError(f"Anthropic could not be reached: {exc}")
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "message", None) or str(exc)
    text = f"Anthropic request failed (HTTP {status}): {detail}"
    if status in (401, 403):
        return ProviderAuthError(text)
    if status == 400 and re.search(r"credit balance|billing|insufficient (credit|funds)", detail, re.I):
        return ProviderQuotaExhausted(text)
    if status == 429:
        retry_after: float | None = None
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            try:
                retry_after = float(headers.get("retry-after"))
            except (TypeError, ValueError):
                retry_after = None
        return ProviderRateLimited(text, retry_after=retry_after)
    if status is not None and status >= 500:
        return ProviderUnavailable(text)
    if status is not None and 400 <= status < 500:
        return ProviderBadRequest(text)
    return ProviderError(text)


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
        # The timeout and the retry count are stated, not inherited from the SDK's defaults.
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key or None,
            timeout=settings.extraction_timeout_seconds,
            max_retries=settings.extraction_max_retries,
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
            raise classify_api_error(exc) from exc
        except TypeError as exc:
            # The SDK raises a bare TypeError when no credential can be found ("Could not resolve
            # authentication method"). That is a configuration problem, not a bug in the request.
            if "authentication" in str(exc).lower():
                raise ProviderAuthError(
                    "No Anthropic credential is configured: set DOCINTEL_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)."
                ) from exc
            raise
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
