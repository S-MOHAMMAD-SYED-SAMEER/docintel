"""The provider boundary and the Anthropic implementation.

No test here reaches the network: the SDK client is replaced with a stub that
records the request and returns a canned response.
"""

import base64
import json
from decimal import Decimal
from typing import Any

import anthropic
import pytest

from app.extractors import Invoice, get_extractor
from app.providers import (
    AnthropicExtractionProvider,
    PageImage,
    ProviderError,
    ProviderRefusal,
    RawExtraction,
    get_provider,
    reset_provider,
)
from app.providers.anthropic_vision import estimate_cost_usd, json_schema_for

from .conftest import VALID_INVOICE_PAYLOAD, FakeProvider, anthropic_response

PAGES = [
    PageImage(page_number=1, media_type="image/png", data=b"\x89PNG-page-one"),
    PageImage(page_number=2, media_type="image/png", data=b"\x89PNG-page-two"),
]


class StubMessages:
    def __init__(self, response: Any, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.request: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.request = kwargs
        if self._error is not None:
            raise self._error
        return self._response


class StubClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.messages = StubMessages(response, error)


class StubResponse:
    """Stands in for an SDK Message, which exposes `to_dict()`."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def to_dict(self) -> dict[str, Any]:
        return self._body


def _provider(response_body: dict[str, Any] | None = None, error=None):
    client = StubClient(
        StubResponse(response_body) if response_body is not None else None, error
    )
    return AnthropicExtractionProvider(client=client, model="claude-opus-5"), client


# --- the interface itself -------------------------------------------------


def test_fake_provider_satisfies_the_interface() -> None:
    """Anything with `extract(images, schema, prompt)` is a provider."""
    provider = FakeProvider()

    raw = provider.extract(PAGES, Invoice, "prompt")

    assert isinstance(raw, RawExtraction)
    assert json.loads(raw.content) == VALID_INVOICE_PAYLOAD


def test_raw_extraction_defaults_metadata_to_empty() -> None:
    raw = RawExtraction(content="{}", raw_response={}, model_name="m")

    assert raw.metadata == {}
    assert raw.input_tokens is None
    assert raw.cost_usd is None


def test_get_provider_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCINTEL_ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    reset_provider()
    try:
        assert get_provider() is get_provider()
    finally:
        reset_provider()


# --- request construction -------------------------------------------------


def test_sends_one_image_block_per_page_then_the_prompt() -> None:
    provider, client = _provider(
        anthropic_response(json.dumps(VALID_INVOICE_PAYLOAD))
    )
    extractor = get_extractor("invoice")

    provider.extract(PAGES, extractor.schema, extractor.prompt)

    content = client.messages.request["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "image", "text"]
    assert content[2]["text"] == extractor.prompt


def test_pages_are_sent_in_page_order_and_base64_encoded() -> None:
    provider, client = _provider(anthropic_response("{}"))

    provider.extract(list(reversed(PAGES)), Invoice, "prompt")

    content = client.messages.request["messages"][0]["content"]
    assert content[0]["source"]["data"] == base64.standard_b64encode(
        b"\x89PNG-page-one"
    ).decode()
    assert content[1]["source"]["data"] == base64.standard_b64encode(
        b"\x89PNG-page-two"
    ).decode()
    assert content[0]["source"]["media_type"] == "image/png"


def test_asks_for_the_schema_via_structured_outputs() -> None:
    provider, client = _provider(anthropic_response("{}"))

    provider.extract(PAGES, Invoice, "prompt")

    output_config = client.messages.request["output_config"]
    assert output_config["format"]["type"] == "json_schema"
    assert set(output_config["format"]["schema"]["properties"]) == set(
        Invoice.model_fields
    )


def test_model_and_max_tokens_come_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DOCINTEL_EXTRACTION_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("DOCINTEL_EXTRACTION_MAX_TOKENS", "2048")
    get_settings.cache_clear()
    try:
        client = StubClient(StubResponse(anthropic_response("{}")))
        provider = AnthropicExtractionProvider(client=client)
        provider.extract(PAGES, Invoice, "prompt")
    finally:
        get_settings.cache_clear()

    assert client.messages.request["model"] == "claude-haiku-4-5"
    assert client.messages.request["max_tokens"] == 2048


def test_no_pages_is_refused_before_calling_the_model() -> None:
    provider, client = _provider(anthropic_response("{}"))

    with pytest.raises(ProviderError, match="no pages"):
        provider.extract([], Invoice, "prompt")

    assert client.messages.request is None


# --- response handling ----------------------------------------------------


def test_returns_content_usage_and_latency() -> None:
    body = anthropic_response(
        json.dumps(VALID_INVOICE_PAYLOAD), input_tokens=4210, output_tokens=388
    )
    provider, _ = _provider(body)

    raw = provider.extract(PAGES, Invoice, "prompt")

    assert json.loads(raw.content) == VALID_INVOICE_PAYLOAD
    assert raw.model_name == "claude-opus-5"
    assert raw.input_tokens == 4210
    assert raw.output_tokens == 388
    assert raw.latency_ms is not None and raw.latency_ms >= 0


def test_raw_response_is_kept_verbatim() -> None:
    body = anthropic_response(json.dumps(VALID_INVOICE_PAYLOAD))
    provider, _ = _provider(body)

    raw = provider.extract(PAGES, Invoice, "prompt")

    assert raw.raw_response == body
    assert raw.raw_response["id"] == "msg_01FakeExtraction"


def test_cost_is_computed_from_usage() -> None:
    provider, _ = _provider(
        anthropic_response("{}", input_tokens=1_000_000, output_tokens=1_000_000)
    )

    raw = provider.extract(PAGES, Invoice, "prompt")

    # claude-opus-5: $5 in + $25 out per million tokens.
    assert raw.cost_usd == Decimal("30.000000")


def test_cost_is_none_for_an_unpriced_model() -> None:
    provider, _ = _provider(anthropic_response("{}", model="some-future-model"))

    raw = provider.extract(PAGES, Invoice, "prompt")

    assert raw.cost_usd is None
    assert raw.model_name == "some-future-model"


def test_cost_is_none_without_usage() -> None:
    assert estimate_cost_usd("claude-opus-5", None, None) is None


def test_refusal_raises_rather_than_returning_nothing() -> None:
    provider, _ = _provider(
        anthropic_response(
            "", stop_reason="refusal", stop_details={"category": "cyber"}
        )
    )

    with pytest.raises(ProviderRefusal, match="cyber"):
        provider.extract(PAGES, Invoice, "prompt")


def test_truncated_response_raises() -> None:
    provider, _ = _provider(
        anthropic_response('{"invoice_number": {"val', stop_reason="max_tokens")
    )

    with pytest.raises(ProviderError, match="cut off"):
        provider.extract(PAGES, Invoice, "prompt")


def test_empty_response_raises() -> None:
    provider, _ = _provider(anthropic_response("   "))

    with pytest.raises(ProviderError, match="no text content"):
        provider.extract(PAGES, Invoice, "prompt")


def test_sdk_errors_are_wrapped_as_provider_errors() -> None:
    error = anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]
    provider, _ = _provider(error=error)

    with pytest.raises(ProviderError, match="Anthropic request failed"):
        provider.extract(PAGES, Invoice, "prompt")


# --- schema translation ---------------------------------------------------


def test_json_schema_closes_every_object() -> None:
    schema = json_schema_for(Invoice)

    assert schema["additionalProperties"] is False
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False


def test_json_schema_requires_every_property() -> None:
    """Nullable is not optional: the model answers every field, with null if absent."""
    schema = json_schema_for(Invoice)

    assert set(schema["required"]) == set(schema["properties"])
    line_item = schema["$defs"]["InvoiceLineItem"]
    assert set(line_item["required"]) == set(line_item["properties"])


def test_money_is_requested_as_a_string_not_a_float() -> None:
    """A JSON number would arrive as a float and stop being exact."""
    schema = json_schema_for(Invoice)
    money_field = next(
        definition
        for name, definition in schema["$defs"].items()
        if "Decimal" in name
    )
    types = {option.get("type") for option in money_field["properties"]["value"]["anyOf"]}

    assert types == {"string", "null"}


def test_the_suite_cannot_reach_the_real_api() -> None:
    """Guard the guard: a stray real request must fail, not bill the account."""
    client = anthropic.Anthropic(api_key="sk-ant-not-a-real-key")

    with pytest.raises(AssertionError, match="real Anthropic API request"):
        client.post("/v1/messages", cast_to=dict, body={})
