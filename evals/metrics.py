"""Metric definitions.

Pure functions over observations. Nothing here reads the database, calls a
provider, or knows what a good number looks like — an observation is what the
pipeline produced, the label is what the document says, and the arithmetic
between them is all that happens.

Normalisation (see `normalise`) removes presentation only:

  * `line_items`   compared structurally, row by row, with amounts as numbers
  * money fields   compared as decimals, so 1,210.00 == 1210.0 == 1210.00
  * date fields    compared as calendar dates
  * everything else  whitespace trimmed and runs collapsed; case and
                     punctuation are NOT touched, because "ACME" and "Acme" are
                     different answers on an invoice

An absent value matches only another absent value.
"""

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

MONEY_FIELDS = frozenset(
    {"subtotal", "tax", "total", "quantity", "unit_price", "amount"}
)
DATE_FIELDS = frozenset({"invoice_date", "due_date"})
STRUCTURED_FIELDS = frozenset({"line_items"})

_WHITESPACE = re.compile(r"\s+")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")


def normalise(field_name: str, value: Any) -> Any:
    """The comparable form of a value. See the module docstring."""
    if value is None:
        return None
    if field_name in STRUCTURED_FIELDS:
        return _normalise_rows(value)
    if field_name in MONEY_FIELDS:
        return _normalise_money(value)
    if field_name in DATE_FIELDS:
        return _normalise_text(value)
    return _normalise_text(value)


def _normalise_text(value: Any) -> str:
    return _WHITESPACE.sub(" ", str(value)).strip()


def _normalise_money(value: Any) -> Any:
    text = _normalise_text(value)
    try:
        return Decimal(_THOUSANDS.sub("", text))
    except (InvalidOperation, ValueError):
        # Not a number after all; fall back to a text comparison rather than
        # calling two unparseable strings equal.
        return text


def _normalise_rows(value: Any) -> Any:
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return _normalise_text(value)

    if not isinstance(value, list):
        return _normalise_text(value)

    rows = []
    for row in value:
        if not isinstance(row, dict):
            rows.append(_normalise_text(row))
            continue
        rows.append(
            tuple(
                sorted(
                    (key, normalise(key, cell))
                    for key, cell in row.items()
                )
            )
        )
    return rows


def matches(field_name: str, extracted: Any, expected: Any) -> bool:
    return normalise(field_name, extracted) == normalise(field_name, expected)


@dataclass(frozen=True)
class FieldObservation:
    """One labelled field of one document, after the pipeline ran."""

    document_id: str
    field_name: str
    expected: Any
    extracted: Any
    # Whether the pipeline routed this field to a human.
    needs_review: bool
    confidence: float
    model_confidence: float
    # True when the document never produced a record. Such a field can never
    # be correct: the pipeline did not answer "absent", it did not answer at
    # all, and letting a null label match nothing would flatter every rate.
    extraction_failed: bool = False

    @property
    def correct(self) -> bool:
        if self.extraction_failed:
            return False
        return matches(self.field_name, self.extracted, self.expected)

    @property
    def false_confident(self) -> bool:
        """Wrong, and the pipeline did not ask anyone to look at it."""
        return not self.correct and not self.needs_review


@dataclass(frozen=True)
class DocumentObservation:
    document_id: str
    fields: tuple[FieldObservation, ...] = ()
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    # Set when extraction never produced a usable record. Its labelled fields
    # still count as wrong; the failure is also reported separately.
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def needs_review(self) -> bool:
        return any(observation.needs_review for observation in self.fields)


@dataclass(frozen=True)
class FieldAccuracy:
    field_name: str
    evaluated: int
    correct: int

    @property
    def accuracy(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.correct / self.evaluated

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "correct": self.correct,
            "accuracy": self.accuracy,
        }


@dataclass(frozen=True)
class Report:
    dataset_name: str
    model_name: str | None
    prompt_version: str | None
    documents: int
    failed_documents: tuple[str, ...]
    fields_evaluated: int
    field_accuracy: dict[str, FieldAccuracy] = field(default_factory=dict)
    # Counts of the four outcomes, so "wrong" is never confused with
    # "wrong and nobody was told".
    correct_confident: int = 0
    correct_flagged: int = 0
    wrong_flagged: int = 0
    wrong_confident: int = 0
    fields_flagged: int = 0
    documents_flagged: int = 0
    total_cost_usd: Decimal | None = None
    documents_with_cost: int = 0
    latencies_ms: tuple[int, ...] = ()

    # --- rates, each with an explicit denominator ---

    @property
    def overall_accuracy(self) -> float | None:
        if self.fields_evaluated == 0:
            return None
        return (self.correct_confident + self.correct_flagged) / self.fields_evaluated

    @property
    def review_rate(self) -> float | None:
        """Flagged fields / evaluated fields."""
        if self.fields_evaluated == 0:
            return None
        return self.fields_flagged / self.fields_evaluated

    @property
    def document_review_rate(self) -> float | None:
        """Documents with at least one flagged field / documents."""
        if self.documents == 0:
            return None
        return self.documents_flagged / self.documents

    @property
    def false_confident_rate(self) -> float | None:
        """Wrong and unflagged / evaluated fields. The headline number."""
        if self.fields_evaluated == 0:
            return None
        return self.wrong_confident / self.fields_evaluated

    @property
    def false_confident_rate_of_unflagged(self) -> float | None:
        """Wrong and unflagged / unflagged fields — how much to trust a pass."""
        unflagged = self.correct_confident + self.wrong_confident
        if unflagged == 0:
            return None
        return self.wrong_confident / unflagged

    # --- cost and latency ---

    @property
    def mean_cost_usd(self) -> Decimal | None:
        if self.total_cost_usd is None or self.documents_with_cost == 0:
            return None
        return (self.total_cost_usd / self.documents_with_cost).quantize(
            Decimal("0.000001")
        )

    @property
    def mean_latency_ms(self) -> float | None:
        if not self.latencies_ms:
            return None
        return sum(self.latencies_ms) / len(self.latencies_ms)

    @property
    def p50_latency_ms(self) -> int | None:
        return percentile(self.latencies_ms, 50)

    @property
    def p95_latency_ms(self) -> int | None:
        return percentile(self.latencies_ms, 95)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset_name,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "documents": self.documents,
            "failed_documents": list(self.failed_documents),
            "fields_evaluated": self.fields_evaluated,
            "overall_accuracy": self.overall_accuracy,
            "field_accuracy": {
                name: accuracy.as_dict()
                for name, accuracy in self.field_accuracy.items()
            },
            "outcomes": {
                "correct_confident": self.correct_confident,
                "correct_flagged": self.correct_flagged,
                "wrong_flagged": self.wrong_flagged,
                "wrong_confident": self.wrong_confident,
            },
            "review_rate": self.review_rate,
            "document_review_rate": self.document_review_rate,
            "false_confident_rate": self.false_confident_rate,
            "false_confident_rate_of_unflagged": (
                self.false_confident_rate_of_unflagged
            ),
            "cost": {
                "total_usd": (
                    None if self.total_cost_usd is None else str(self.total_cost_usd)
                ),
                "mean_usd_per_document": (
                    None if self.mean_cost_usd is None else str(self.mean_cost_usd)
                ),
                "documents_with_cost": self.documents_with_cost,
            },
            "latency_ms": {
                "documents_with_latency": len(self.latencies_ms),
                "mean": self.mean_latency_ms,
                "p50": self.p50_latency_ms,
                "p95": self.p95_latency_ms,
            },
        }


def percentile(values: tuple[int, ...] | list[int], which: int) -> int | None:
    """Nearest-rank percentile: the smallest value at or above `which` percent.

    Nearest-rank rather than an interpolating variant so the result is always
    a latency that was actually observed.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(which / 100 * len(ordered)))
    return ordered[rank - 1]


def summarise(
    dataset_name: str, observations: list[DocumentObservation]
) -> Report:
    """Fold document observations into one report."""
    accuracy: dict[str, tuple[int, int]] = {}
    correct_confident = correct_flagged = wrong_flagged = wrong_confident = 0
    fields_evaluated = fields_flagged = documents_flagged = 0
    costs: list[Decimal] = []
    latencies: list[int] = []
    failed: list[str] = []
    model_name: str | None = None
    prompt_version: str | None = None

    for document in observations:
        if document.failed:
            failed.append(document.document_id)
        if document.model_name and model_name is None:
            model_name = document.model_name
        if document.prompt_version and prompt_version is None:
            prompt_version = document.prompt_version
        if document.cost_usd is not None:
            costs.append(document.cost_usd)
        if document.latency_ms is not None:
            latencies.append(document.latency_ms)
        if document.needs_review:
            documents_flagged += 1

        for observation in document.fields:
            fields_evaluated += 1
            evaluated, correct = accuracy.get(observation.field_name, (0, 0))
            accuracy[observation.field_name] = (
                evaluated + 1,
                correct + int(observation.correct),
            )

            if observation.needs_review:
                fields_flagged += 1
                if observation.correct:
                    correct_flagged += 1
                else:
                    wrong_flagged += 1
            elif observation.correct:
                correct_confident += 1
            else:
                wrong_confident += 1

    return Report(
        dataset_name=dataset_name,
        model_name=model_name,
        prompt_version=prompt_version,
        documents=len(observations),
        failed_documents=tuple(failed),
        fields_evaluated=fields_evaluated,
        field_accuracy={
            name: FieldAccuracy(name, evaluated, correct)
            for name, (evaluated, correct) in accuracy.items()
        },
        correct_confident=correct_confident,
        correct_flagged=correct_flagged,
        wrong_flagged=wrong_flagged,
        wrong_confident=wrong_confident,
        fields_flagged=fields_flagged,
        documents_flagged=documents_flagged,
        total_cost_usd=sum(costs, Decimal(0)) if costs else None,
        documents_with_cost=len(costs),
        latencies_ms=tuple(latencies),
    )
