"""The model-call boundary.

Everything above this line works in terms of `extract(images, schema, prompt)`
and `RawExtraction`. Nothing above it imports a vendor SDK, so swapping or
adding a provider touches only `app/providers/`.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class PageImage:
    """One rendered page, ready to send to a vision model.

    Bytes rather than a path: a provider may well be a remote service, and the
    caller should not assume the model runs on this filesystem.
    """

    page_number: int
    media_type: str
    data: bytes


@dataclass(frozen=True)
class RawExtraction:
    """What a provider returns: the model's answer plus what it cost.

    `raw_response` is the provider's complete response, kept verbatim. It is
    never discarded — debugging a bad extraction and re-scoring old runs both
    need the original.
    """

    content: str
    raw_response: dict[str, Any]
    model_name: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    # Free-form provider notes (stop reason, refusal category, ...) that do not
    # belong in the columns above.
    metadata: dict[str, Any] = field(default_factory=dict)


class ExtractionProvider(Protocol):
    """The one call the pipeline makes to a model."""

    def extract(
        self,
        images: Sequence[PageImage],
        schema: type[BaseModel],
        prompt: str,
    ) -> RawExtraction: ...


class ProviderError(Exception):
    """The provider could not produce an answer.

    Raised rather than returned: a failed extraction must be recorded as a
    failure, never as an empty result.
    """


class ProviderRefusal(ProviderError):
    """The model declined to answer."""


class ProviderAuthError(ProviderError):
    """No credential, or one the service rejected (401, 403). Retrying cannot help."""


class ProviderQuotaExhausted(ProviderAuthError):
    """The account has no credit or quota left. Retrying cannot help until it is topped up."""


class ProviderRateLimited(ProviderError):
    """The service said to slow down (429). `retry_after` is how long it asked, in seconds, when it said."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    """The service is overloaded or failing (500, 503, 529) and the retries did not clear it."""


class ProviderTimeout(ProviderError):
    """The service did not answer within the configured time, even after the retries."""


class ProviderConnectionError(ProviderError):
    """The service could not be reached."""


class ProviderBadRequest(ProviderError):
    """The service rejected the request itself (400, 404, 413, 422): a bug or a wrong model ID, not a blip."""


#: Failures after which a counted run must stop at once instead of going on to the next document: the next
#: request would fail the same way, and an overloaded or rate-limited service should be left alone.
STOP_RULE_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderTimeout,
    ProviderConnectionError,
    ProviderBadRequest,
)
