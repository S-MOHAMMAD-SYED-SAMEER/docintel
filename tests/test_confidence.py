"""Final per-field confidence and the needs_review decision.

Pure scoring: no database, no provider, no I/O.
"""

import json

import pytest

from app.config import Settings
from app.confidence import (
    ARITHMETIC,
    MODEL,
    SCHEMA,
    TEXT_LAYER,
    is_text_comparable,
    score_extraction,
    score_field,
)
from app.extraction import _as_text
from app.extractors import Invoice
from app.validation import get_validator
from app.validation.text_layer import TextLayer

from .conftest import VALID_INVOICE_PAYLOAD

NO_TEXT_LAYER = TextLayer()
TEXT_LAYER_PAGES = TextLayer.from_pages(
    [
        "Acme Supplies BV   Invoice INV-2026-0042   NL123456789B01",
        "Beta Ltd  Subtotal 1,000.00  Tax 210.00  Total EUR 1,210.00",
    ]
)


def _invoice(**overrides: object) -> Invoice:
    payload = json.loads(json.dumps(VALID_INVOICE_PAYLOAD))
    for field_name, value in overrides.items():
        payload[field_name] |= value if isinstance(value, dict) else {"value": value}
    return Invoice.model_validate(payload)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def _score(invoice: Invoice, field_name: str, layer=NO_TEXT_LAYER, settings=None):
    report = get_validator("invoice")(invoice)
    texts = {name: _as_text(getattr(invoice, name).value) for name in Invoice.model_fields}
    return score_field(
        field_name,
        getattr(invoice, field_name),
        texts[field_name],
        report,
        layer,
        settings or _settings(),
    )


def _signal(score, name: str):
    return next((signal for signal in score.signals if signal.name == name), None)


# --- signal applicability -------------------------------------------------


def test_the_model_signal_always_applies() -> None:
    score = _score(_invoice(), "customer_name")

    assert _signal(score, MODEL) is not None
    assert _signal(score, MODEL).score == pytest.approx(0.9)


def test_a_field_with_no_deterministic_checks_falls_back_to_the_model() -> None:
    """Nothing else is known about it, so there is nothing else to combine."""
    score = _score(_invoice(), "customer_name")

    assert [signal.name for signal in score.signals] == [MODEL]
    assert score.confidence == pytest.approx(0.9)


def test_skipped_checks_do_not_drag_a_field_down() -> None:
    """No subtotal means the arithmetic could not run, not that it failed."""
    score = _score(_invoice(subtotal=None, total=None), "tax")

    assert _signal(score, ARITHMETIC) is None
    assert score.confidence == pytest.approx(0.91)


# --- high model confidence ------------------------------------------------


def test_high_model_confidence_with_all_checks_passing_scores_high() -> None:
    score = _score(_invoice(), "total", TEXT_LAYER_PAGES)

    # model 0.98 * 0.4 + arithmetic 1.0 * 0.2 + text layer 1.0 * 0.2, over 0.8.
    assert score.confidence == pytest.approx((0.4 * 0.98 + 0.2 + 0.2) / 0.8)
    assert score.confidence > 0.98
    assert score.needs_review is False


def test_high_model_confidence_with_a_deterministic_failure_is_not_trusted() -> None:
    """The model saying 0.98 does not make 1000 + 210 = 1500."""
    invoice = _invoice(total="1500.00")

    score = _score(invoice, "total", TEXT_LAYER_PAGES)

    assert _signal(score, ARITHMETIC).score == 0.0
    assert score.confidence < 0.85
    assert score.needs_review is True
    assert "totals.subtotal_plus_tax_equals_total" in score.blocking_failures


def test_a_deterministic_failure_forces_review_whatever_the_score() -> None:
    """Weights could put a failing field above the threshold; review wins."""
    settings = _settings(
        confidence_weight_model=1.0,
        confidence_weight_schema=0.0,
        confidence_weight_arithmetic=0.0,
        confidence_weight_text_layer=0.0,
    )
    invoice = _invoice(total="1500.00")

    score = _score(invoice, "total", NO_TEXT_LAYER, settings)

    assert score.confidence == pytest.approx(0.98)
    assert score.confidence >= settings.confidence_threshold
    assert score.needs_review is True
    assert score.blocking_failures


def test_a_schema_failure_also_forces_review() -> None:
    score = _score(_invoice(currency="EURO"), "currency")

    assert _signal(score, SCHEMA).score == 0.0
    assert score.needs_review is True
    assert "currency.iso_4217" in score.blocking_failures


# --- low model confidence -------------------------------------------------


def test_low_model_confidence_is_lifted_but_not_rescued_by_passing_checks() -> None:
    invoice = _invoice(total={"confidence": 0.2})

    score = _score(invoice, "total", TEXT_LAYER_PAGES)

    # (0.4*0.2 + 0.2 + 0.2) / 0.8 = 0.6 — better than 0.2, still below 0.85.
    assert score.confidence == pytest.approx(0.6)
    assert score.needs_review is True
    assert score.blocking_failures == ()


def test_low_model_confidence_with_no_other_signal_stays_low() -> None:
    score = _score(_invoice(), "purchase_order_number")

    assert score.confidence == pytest.approx(0.2)
    assert score.needs_review is True


# --- text layer -----------------------------------------------------------


def test_a_value_present_in_the_text_layer_scores_the_signal_one() -> None:
    score = _score(_invoice(), "invoice_number", TEXT_LAYER_PAGES)

    assert _signal(score, TEXT_LAYER).score == 1.0
    assert _signal(score, TEXT_LAYER).detail == "found"


def test_a_value_absent_from_the_text_layer_scores_the_signal_zero() -> None:
    invoice = _invoice(invoice_number="INV-9999-0001")

    score = _score(invoice, "invoice_number", TEXT_LAYER_PAGES)

    assert _signal(score, TEXT_LAYER).score == 0.0
    assert score.confidence < _score(_invoice(), "invoice_number",
                                     TEXT_LAYER_PAGES).confidence


def test_a_text_layer_miss_lowers_the_score_but_does_not_force_review() -> None:
    """Imperfect text layers make false negatives common, so it only weighs in."""
    invoice = _invoice(invoice_number="INV-9999-0001")

    score = _score(invoice, "invoice_number", TEXT_LAYER_PAGES)

    assert score.blocking_failures == ()
    # (0.4*0.97 + 0.2*1 + 0.2*0) / 0.8 = 0.735
    assert score.confidence == pytest.approx(0.735)
    assert score.needs_review is True  # by threshold, not by force


def test_no_text_layer_means_the_signal_simply_does_not_apply() -> None:
    """A scanned invoice must not be punished for being scanned."""
    with_layer = _score(_invoice(), "invoice_number", TEXT_LAYER_PAGES)
    without = _score(_invoice(), "invoice_number", NO_TEXT_LAYER)

    assert _signal(without, TEXT_LAYER) is None
    assert without.confidence == pytest.approx((0.4 * 0.97 + 0.2) / 0.6)
    assert without.confidence > 0.85
    assert with_layer.confidence > 0.85


def test_line_items_are_not_looked_for_in_the_text_layer() -> None:
    """Their serialised JSON appears nowhere in the document."""
    score = _score(_invoice(), "line_items", TEXT_LAYER_PAGES)

    assert _signal(score, TEXT_LAYER) is None


def test_a_null_value_is_not_looked_for_in_the_text_layer() -> None:
    score = _score(_invoice(), "purchase_order_number", TEXT_LAYER_PAGES)

    assert _signal(score, TEXT_LAYER) is None


@pytest.mark.parametrize(
    ("value", "comparable"),
    [("INV-1", True), (1, True), (True, False), ([], False), (None, False)],
)
def test_is_text_comparable(value: object, comparable: bool) -> None:
    assert is_text_comparable(value) is comparable


# --- weights --------------------------------------------------------------


def test_default_weights_give_the_model_less_than_half_the_say() -> None:
    settings = _settings()

    deterministic = (
        settings.confidence_weight_schema
        + settings.confidence_weight_arithmetic
        + settings.confidence_weight_text_layer
    )
    assert settings.confidence_weight_model == pytest.approx(0.4)
    assert deterministic > settings.confidence_weight_model


def test_changing_the_weights_changes_the_score() -> None:
    invoice = _invoice(total="1500.00")

    trusting = _score(
        invoice, "total", NO_TEXT_LAYER, _settings(confidence_weight_model=0.9)
    )
    sceptical = _score(
        invoice, "total", NO_TEXT_LAYER, _settings(confidence_weight_model=0.1)
    )

    assert trusting.confidence > sceptical.confidence


def test_weights_are_renormalised_over_the_applicable_signals() -> None:
    """Weights need not sum to 1; only the applicable ones are used."""
    settings = _settings(
        confidence_weight_model=2.0,
        confidence_weight_schema=1.0,
        confidence_weight_arithmetic=1.0,
        confidence_weight_text_layer=1.0,
    )

    score = _score(_invoice(), "total", NO_TEXT_LAYER, settings)

    # Only model and arithmetic apply: (2*0.98 + 1*1.0) / 3.
    assert score.confidence == pytest.approx((2 * 0.98 + 1.0) / 3)


def test_all_zero_weights_fall_back_to_the_model() -> None:
    settings = _settings(
        confidence_weight_model=0.0,
        confidence_weight_schema=0.0,
        confidence_weight_arithmetic=0.0,
        confidence_weight_text_layer=0.0,
    )

    score = _score(_invoice(), "total", NO_TEXT_LAYER, settings)

    assert score.confidence == pytest.approx(0.98)


# --- threshold ------------------------------------------------------------


def test_default_threshold_is_the_readme_value() -> None:
    assert _settings().confidence_threshold == 0.85


def test_raising_the_threshold_flags_more_fields() -> None:
    invoice = _invoice()
    lenient = score_extraction(
        invoice,
        get_validator("invoice")(invoice),
        NO_TEXT_LAYER,
        {name: _as_text(getattr(invoice, name).value) for name in Invoice.model_fields},
        _settings(confidence_threshold=0.5),
    )
    strict = score_extraction(
        invoice,
        get_validator("invoice")(invoice),
        NO_TEXT_LAYER,
        {name: _as_text(getattr(invoice, name).value) for name in Invoice.model_fields},
        _settings(confidence_threshold=0.99),
    )

    lenient_flagged = {name for name, s in lenient.items() if s.needs_review}
    strict_flagged = {name for name, s in strict.items() if s.needs_review}
    assert lenient_flagged < strict_flagged


def test_threshold_boundary_is_inclusive_at_the_threshold() -> None:
    """At the threshold a field passes; a hair below it does not."""
    invoice = _invoice(customer_name={"confidence": 0.85})

    at = _score(invoice, "customer_name", NO_TEXT_LAYER, _settings())
    assert at.confidence == pytest.approx(0.85)
    assert at.needs_review is False

    below = _score(
        _invoice(customer_name={"confidence": 0.8499}),
        "customer_name",
        NO_TEXT_LAYER,
        _settings(),
    )
    assert below.needs_review is True


# --- shape ----------------------------------------------------------------


def test_every_field_is_scored_independently() -> None:
    invoice = _invoice()
    scores = score_extraction(
        invoice,
        get_validator("invoice")(invoice),
        NO_TEXT_LAYER,
        {name: _as_text(getattr(invoice, name).value) for name in Invoice.model_fields},
        _settings(),
    )

    assert set(scores) == set(Invoice.model_fields)
    assert len({score.confidence for score in scores.values()}) > 1


def test_the_model_confidence_is_carried_through_untouched() -> None:
    score = _score(_invoice(), "total", TEXT_LAYER_PAGES)

    assert score.model_confidence == pytest.approx(0.98)
    assert score.confidence != score.model_confidence


def test_scoring_is_deterministic() -> None:
    first = _score(_invoice(), "total", TEXT_LAYER_PAGES).as_dict()
    second = _score(_invoice(), "total", TEXT_LAYER_PAGES).as_dict()

    assert first == second


def test_the_breakdown_records_every_signal_and_its_weight() -> None:
    breakdown = _score(_invoice(), "total", TEXT_LAYER_PAGES).as_dict()

    names = {signal["name"] for signal in breakdown["signals"]}
    assert names == {MODEL, ARITHMETIC, TEXT_LAYER}
    assert all("weight" in signal for signal in breakdown["signals"])
    assert breakdown["model_confidence"] == pytest.approx(0.98)
