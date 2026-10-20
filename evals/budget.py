"""A hard cap on what an evaluation run may spend, kept on disk.

`python -m evals.run --provider anthropic --request-cap 80 --max-cost-usd 2.00` refuses to send request
number 81, and refuses to send a request that would take the total past $2.00. The count and the spend live
in a small ledger file, not in the process, so they survive a run that is interrupted and are shared by every
run that points at the same ledger: the cap is for the whole piece of work, not per command.

A request is counted BEFORE it is sent (a request that hangs, fails or is never answered still counts), and
its cost is added to the entry when the answer arrives. Nothing here retries. The first failure that means
"stop" (a bad or missing credential, no credit, a rate limit, an overloaded service, a timeout, a rejected
request) stops the run at once: the next request would fail the same way, and a service that is overloaded
or rate-limited should be left alone.

Adapter retries must be off for a counted run (`DOCINTEL_EXTRACTION_MAX_RETRIES=0`): a retry inside the SDK is
an extra HTTP request this ledger cannot see.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from app.config import get_settings
from app.providers import ExtractionProvider, PageImage, RawExtraction
from app.providers.base import STOP_RULE_ERRORS, ProviderError
from app.providers.pricing import price_for

DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "var" / "eval_ledger.json"

#: Used only for the first request of a ledger, before any real cost has been seen: roughly 2,600 input
#: tokens a page plus 2,000 for the prompt and the schema, and 2,000 output tokens.
ESTIMATE_TOKENS_PER_PAGE = 2_600
ESTIMATE_FIXED_INPUT_TOKENS = 2_000
ESTIMATE_OUTPUT_TOKENS = 2_000


class BudgetRefused(ProviderError):
    """The cap is spent (or would be exceeded). Raised instead of sending; nothing was sent."""


class RunStopped(Exception):
    """The run stopped before every document was done, and why. A partial run is not a result."""

    def __init__(self, reason: str, documents_done: int, documents_total: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.documents_done = documents_done
        self.documents_total = documents_total


class RunBudget:
    def __init__(self, *, request_cap: int, cost_cap_usd: Decimal, path: Path | None = None) -> None:
        self.request_cap = request_cap
        self.cost_cap_usd = cost_cap_usd
        self.path = path or DEFAULT_LEDGER

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"entries": []}

    def _write(self, ledger: dict) -> None:
        ledger["request_cap"] = self.request_cap
        ledger["cost_cap_usd"] = str(self.cost_cap_usd)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    @property
    def requests_used(self) -> int:
        return len(self._read()["entries"])

    @property
    def spent_usd(self) -> Decimal:
        return sum((Decimal(e["cost_usd"]) for e in self._read()["entries"] if e.get("cost_usd")), Decimal(0))

    def largest_cost_seen(self) -> Decimal:
        costs = [Decimal(e["cost_usd"]) for e in self._read()["entries"] if e.get("cost_usd")]
        return max(costs, default=Decimal(0))

    def reserve(self, label: str, *, projected_cost: Decimal) -> int:
        """Count one request and return its index, or raise `BudgetRefused` without counting."""
        ledger = self._read()
        used = len(ledger["entries"])
        if used >= self.request_cap:
            raise BudgetRefused(f"the request cap of {self.request_cap} is spent ({used} used); nothing was sent")
        spent = sum((Decimal(e["cost_usd"]) for e in ledger["entries"] if e.get("cost_usd")), Decimal(0))
        if spent + projected_cost > self.cost_cap_usd:
            raise BudgetRefused(
                f"the cost cap of ${self.cost_cap_usd} would be passed (${spent} spent, the next request "
                f"projected at ${projected_cost}); nothing was sent"
            )
        ledger["entries"].append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "label": label, "cost_usd": None})
        self._write(ledger)
        return used

    def record_cost(self, index: int, cost: Decimal | None, *, model: str | None = None, tokens: tuple[int | None, int | None] = (None, None)) -> None:
        ledger = self._read()
        entry = ledger["entries"][index]
        entry["cost_usd"] = str(cost) if cost is not None else None
        entry["model"] = model
        entry["input_tokens"], entry["output_tokens"] = tokens
        self._write(ledger)


class BudgetedProvider:
    """Counts every `extract()` call against the budget before the inner provider sees it."""

    def __init__(self, inner: ExtractionProvider, budget: RunBudget) -> None:
        self._inner = inner
        self.budget = budget
        #: Set by the first failure that means "stop"; the runner checks it after every document.
        self.halt_reason: str | None = None

    @property
    def model_name(self) -> str:
        return getattr(self._inner, "model_name", get_settings().extraction_model)

    def _projected_cost(self, images: Sequence[PageImage]) -> Decimal:
        seen = self.budget.largest_cost_seen()
        pricing = price_for(self.model_name)
        if pricing is None:
            return seen
        tokens_in = ESTIMATE_FIXED_INPUT_TOKENS + ESTIMATE_TOKENS_PER_PAGE * len(images)
        estimate = (Decimal(tokens_in) * pricing[0] + Decimal(ESTIMATE_OUTPUT_TOKENS) * pricing[1]) / Decimal(1_000_000)
        return max(seen, estimate)

    def extract(self, images: Sequence[PageImage], schema: type[BaseModel], prompt: str) -> RawExtraction:
        if self.halt_reason is not None:
            raise BudgetRefused(f"the run was stopped ({self.halt_reason}); nothing was sent")
        try:
            index = self.budget.reserve("evals", projected_cost=self._projected_cost(images))
        except BudgetRefused as exc:
            self.halt_reason = str(exc)
            raise
        try:
            raw = self._inner.extract(images, schema, prompt)
        except STOP_RULE_ERRORS as exc:
            self.halt_reason = f"{type(exc).__name__}: {exc}"
            raise
        self.budget.record_cost(index, raw.cost_usd, model=raw.model_name, tokens=(raw.input_tokens, raw.output_tokens))
        return raw


__all__ = ["BudgetRefused", "BudgetedProvider", "DEFAULT_LEDGER", "RunBudget", "RunStopped"]
