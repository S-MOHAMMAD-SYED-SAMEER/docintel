"""Final per-field confidence: the README's four signals, weighted.

    1. the model's own self-reported confidence
    2. schema / format validation      (deterministic)
    3. arithmetic consistency          (deterministic)
    4. presence in the page text layer (deterministic, where one exists)

Signals that do not apply to a field are left out and the remaining weights are
renormalised, so a field is never scored down for a check that could not run.
An invoice with no subtotal has not got the arithmetic wrong, and a scanned
invoice has not hidden its total — both simply offer fewer signals.

Scoring is pure: given the same inputs it always produces the same score.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.config import Settings, get_settings
from app.extractors.base import ExtractedField
from app.validation import CheckKind, CheckStatus, ValidationReport
from app.validation.text_layer import TextLayer

logger = logging.getLogger(__name__)

MODEL = "model"
SCHEMA = "schema"
ARITHMETIC = "arithmetic"
TEXT_LAYER = "text_layer"

# Deterministic signals. A FAILED check on one of these forces review however
# the weighted score lands — see `FieldScore.needs_review`.
BLOCKING_SIGNALS = (SCHEMA, ARITHMETIC)


@dataclass(frozen=True)
class Signal:
    """One contribution to a field's score."""

    name: str
    weight: float
    score: float
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "weight": self.weight,
            "score": self.score,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FieldScore:
    field_name: str
    # Kept verbatim. The final score never overwrites what the model said.
    model_confidence: float
    confidence: float
    needs_review: bool
    signals: tuple[Signal, ...]
    # Names of the failed deterministic checks that forced review on their own.
    blocking_failures: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "model_confidence": self.model_confidence,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "signals": [signal.as_dict() for signal in self.signals],
            "blocking_failures": list(self.blocking_failures),
        }


def is_text_comparable(value: Any) -> bool:
    """Can this value sensibly be looked for in a page of text?

    A scalar the model read off the page can be. The line-item table, which is
    stored as a JSON blob, cannot — its serialised form appears nowhere in the
    document.
    """
    return isinstance(value, str | date | Decimal | int) and not isinstance(value, bool)


def score_field(
    field_name: str,
    field: ExtractedField[Any],
    value_text: str | None,
    report: ValidationReport,
    text_layer: TextLayer,
    settings: Settings | None = None,
) -> FieldScore:
    """Combine the applicable signals into one score for one field."""
    settings = settings or get_settings()
    signals: list[Signal] = [
        Signal(MODEL, settings.confidence_weight_model, field.confidence)
    ]

    for signal_name, kind, weight in (
        (SCHEMA, CheckKind.SCHEMA, settings.confidence_weight_schema),
        (ARITHMETIC, CheckKind.ARITHMETIC, settings.confidence_weight_arithmetic),
    ):
        status = report.status_for(field_name, kind)
        if status is CheckStatus.SKIPPED:
            # No check of this kind ran for this field: the signal does not
            # apply, so it is left out rather than scored zero.
            continue
        signals.append(
            Signal(
                signal_name,
                weight,
                1.0 if status is CheckStatus.PASSED else 0.0,
                str(status),
            )
        )

    if text_layer.exists and value_text and is_text_comparable(field.value):
        found = text_layer.contains(value_text)
        signals.append(
            Signal(
                TEXT_LAYER,
                settings.confidence_weight_text_layer,
                1.0 if found else 0.0,
                "found" if found else "not found in the page text layer",
            )
        )

    confidence = _weighted(signals, fallback=field.confidence, field_name=field_name)

    blocking = tuple(
        check.name
        for check in report.for_field(field_name)
        if check.status is CheckStatus.FAILED and str(check.kind) in BLOCKING_SIGNALS
    )
    # Below the threshold, or a deterministic check said the value is wrong. A
    # missing string in the text layer is weaker evidence — reformatting and
    # imperfect text layers make false negatives common — so it lowers the
    # score but never forces review by itself.
    needs_review = confidence < settings.confidence_threshold or bool(blocking)

    return FieldScore(
        field_name=field_name,
        model_confidence=field.confidence,
        confidence=confidence,
        needs_review=needs_review,
        signals=tuple(signals),
        blocking_failures=blocking,
    )


def _weighted(signals: list[Signal], fallback: float, field_name: str) -> float:
    total_weight = sum(signal.weight for signal in signals)
    if total_weight <= 0:
        # Every applicable signal was configured to weigh nothing. There is
        # nothing to combine, so the model's number is all that is left.
        logger.warning(
            "all confidence weights are zero for %s; falling back to the "
            "model's own confidence",
            field_name,
        )
        return fallback

    weighted = sum(signal.weight * signal.score for signal in signals)
    return _clamp(weighted / total_weight)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_extraction(
    parsed: BaseModel,
    report: ValidationReport,
    text_layer: TextLayer,
    value_texts: Mapping[str, str | None],
    settings: Settings | None = None,
) -> dict[str, FieldScore]:
    """Score every field of a parsed extraction, independently of the others."""
    settings = settings or get_settings()
    scores: dict[str, FieldScore] = {}

    for field_name in type(parsed).model_fields:
        field = getattr(parsed, field_name)
        scores[field_name] = score_field(
            field_name,
            field,
            value_texts.get(field_name),
            report,
            text_layer,
            settings,
        )
    return scores
