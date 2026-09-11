"""Metric arithmetic and normalisation. Pure functions, no database."""

from decimal import Decimal

import pytest

from evals.metrics import (
    DocumentObservation,
    FieldObservation,
    matches,
    normalise,
    percentile,
    summarise,
)


def _field(
    name: str = "total",
    expected: object = "100.00",
    extracted: object = "100.00",
    needs_review: bool = False,
    document_id: str = "doc-1",
) -> FieldObservation:
    return FieldObservation(
        document_id=document_id,
        field_name=name,
        expected=expected,
        extracted=extracted,
        needs_review=needs_review,
        confidence=0.9,
        model_confidence=0.9,
    )


def _document(
    fields: list[FieldObservation],
    document_id: str = "doc-1",
    cost: str | None = "0.01",
    latency: int | None = 1000,
    error: str | None = None,
) -> DocumentObservation:
    return DocumentObservation(
        document_id=document_id,
        fields=tuple(fields),
        cost_usd=Decimal(cost) if cost is not None else None,
        latency_ms=latency,
        model_name="fake-model-1",
        prompt_version="invoice/v1",
        error=error,
    )


# --- normalisation --------------------------------------------------------


def test_money_is_compared_numerically() -> None:
    assert matches("total", "1,210.00", "1210.0")
    assert matches("subtotal", "  100.00 ", "100")
    assert not matches("total", "1210.00", "1201.00")


def test_dates_compare_as_written() -> None:
    assert matches("invoice_date", "2026-01-05", "2026-01-05")
    assert not matches("invoice_date", "2026-01-06", "2026-01-05")


def test_whitespace_is_presentation_and_is_normalised() -> None:
    assert matches("vendor_name", "Acme   Supplies\nBV", "Acme Supplies BV")


def test_case_is_meaningful_and_is_not_normalised_away() -> None:
    assert not matches("vendor_name", "ACME SUPPLIES BV", "Acme Supplies BV")


def test_punctuation_is_meaningful_and_is_not_normalised_away() -> None:
    assert not matches("invoice_number", "INV20261001", "INV-2026-1001")


def test_absent_matches_only_absent() -> None:
    assert matches("purchase_order_number", None, None)
    assert not matches("purchase_order_number", None, "PO-1")
    assert not matches("purchase_order_number", "PO-1", None)


def test_line_items_compare_structurally() -> None:
    extracted = '[{"description": "A", "quantity": "2", "unit_price": "5.0", '
    extracted += '"amount": "10.00"}]'
    expected = [
        {"description": "A", "quantity": "2", "unit_price": "5.00", "amount": "10.0"}
    ]

    assert matches("line_items", extracted, expected)


def test_line_item_order_is_meaningful() -> None:
    rows = [{"description": "A"}, {"description": "B"}]

    assert not matches("line_items", list(reversed(rows)), rows)


def test_an_unparseable_money_value_falls_back_to_text() -> None:
    assert normalise("total", "about a hundred") == "about a hundred"
    assert not matches("total", "about a hundred", "100.00")


# --- accuracy -------------------------------------------------------------


def test_zero_evaluated_items() -> None:
    report = summarise("empty", [])

    assert report.documents == 0
    assert report.fields_evaluated == 0
    assert report.overall_accuracy is None
    assert report.review_rate is None
    assert report.false_confident_rate is None
    assert report.mean_cost_usd is None
    assert report.mean_latency_ms is None
    assert report.p50_latency_ms is None


def test_all_correct() -> None:
    report = summarise("d", [_document([_field(), _field(name="tax")])])

    assert report.overall_accuracy == 1.0
    assert report.field_accuracy["total"].accuracy == 1.0
    assert report.wrong_confident == 0
    assert report.false_confident_rate == 0.0


def test_all_wrong() -> None:
    report = summarise("d", [_document([_field(extracted="1.00")])])

    assert report.overall_accuracy == 0.0
    assert report.field_accuracy["total"].correct == 0


def test_per_field_accuracy_is_independent() -> None:
    report = summarise(
        "d",
        [
            _document(
                [
                    _field(name="total", extracted="1.00"),
                    _field(name="tax", expected="21.00", extracted="21.00"),
                ]
            )
        ],
    )

    assert report.field_accuracy["total"].accuracy == 0.0
    assert report.field_accuracy["tax"].accuracy == 1.0


def test_mixed_field_availability_across_documents() -> None:
    """A field only some documents label is scored on those documents only."""
    report = summarise(
        "d",
        [
            _document([_field(name="total"), _field(name="tax", expected="1", extracted="1")]),
            _document([_field(name="total")], document_id="doc-2"),
        ],
    )

    assert report.field_accuracy["total"].evaluated == 2
    assert report.field_accuracy["tax"].evaluated == 1


# --- the four outcomes ----------------------------------------------------


def test_wrong_but_flagged_is_caught_not_false_confident() -> None:
    report = summarise(
        "d", [_document([_field(extracted="1.00", needs_review=True)])]
    )

    assert report.wrong_flagged == 1
    assert report.wrong_confident == 0
    assert report.false_confident_rate == 0.0


def test_wrong_and_unflagged_is_false_confident() -> None:
    report = summarise(
        "d", [_document([_field(extracted="1.00", needs_review=False)])]
    )

    assert report.wrong_confident == 1
    assert report.wrong_flagged == 0
    assert report.false_confident_rate == 1.0


def test_correct_but_flagged_is_not_false_confident() -> None:
    report = summarise("d", [_document([_field(needs_review=True)])])

    assert report.correct_flagged == 1
    assert report.false_confident_rate == 0.0


def test_the_four_outcomes_partition_the_fields() -> None:
    report = summarise(
        "d",
        [
            _document(
                [
                    _field(name="a"),
                    _field(name="b", needs_review=True),
                    _field(name="c", extracted="1.00", needs_review=True),
                    _field(name="d", extracted="1.00"),
                ]
            )
        ],
    )

    assert (
        report.correct_confident
        + report.correct_flagged
        + report.wrong_flagged
        + report.wrong_confident
    ) == report.fields_evaluated
    assert (report.correct_confident, report.correct_flagged) == (1, 1)
    assert (report.wrong_flagged, report.wrong_confident) == (1, 1)


def test_false_confident_rate_of_unflagged_uses_the_narrower_denominator() -> None:
    report = summarise(
        "d",
        [
            _document(
                [
                    _field(name="a"),
                    _field(name="b", extracted="1.00"),
                    _field(name="c", extracted="1.00", needs_review=True),
                ]
            )
        ],
    )

    assert report.false_confident_rate == pytest.approx(1 / 3)
    assert report.false_confident_rate_of_unflagged == pytest.approx(1 / 2)


# --- review rate ----------------------------------------------------------


def test_review_rate_counts_fields() -> None:
    report = summarise(
        "d",
        [_document([_field(name="a", needs_review=True), _field(name="b")])],
    )

    assert report.review_rate == 0.5
    assert report.fields_flagged == 1


def test_document_review_rate_counts_documents_with_any_flagged_field() -> None:
    report = summarise(
        "d",
        [
            _document([_field(name="a", needs_review=True), _field(name="b")]),
            _document([_field(name="a")], document_id="doc-2"),
        ],
    )

    assert report.document_review_rate == 0.5
    assert report.review_rate == pytest.approx(1 / 3)


# --- cost -----------------------------------------------------------------


def test_cost_is_summed_and_averaged() -> None:
    report = summarise(
        "d",
        [
            _document([_field()], cost="0.02"),
            _document([_field()], document_id="doc-2", cost="0.04"),
        ],
    )

    assert report.total_cost_usd == Decimal("0.06")
    assert report.mean_cost_usd == Decimal("0.030000")
    assert report.documents_with_cost == 2


def test_missing_cost_is_not_invented() -> None:
    report = summarise("d", [_document([_field()], cost=None)])

    assert report.total_cost_usd is None
    assert report.mean_cost_usd is None
    assert report.documents_with_cost == 0


def test_partial_cost_averages_only_over_documents_that_reported_it() -> None:
    report = summarise(
        "d",
        [
            _document([_field()], cost="0.02"),
            _document([_field()], document_id="doc-2", cost=None),
        ],
    )

    assert report.mean_cost_usd == Decimal("0.020000")
    assert report.documents_with_cost == 1


# --- latency --------------------------------------------------------------


def test_mean_latency() -> None:
    report = summarise(
        "d",
        [
            _document([_field()], latency=1000),
            _document([_field()], document_id="doc-2", latency=3000),
        ],
    )

    assert report.mean_latency_ms == 2000.0


def test_missing_latency_is_not_invented() -> None:
    report = summarise("d", [_document([_field()], latency=None)])

    assert report.mean_latency_ms is None
    assert report.p50_latency_ms is None
    assert report.p95_latency_ms is None


@pytest.mark.parametrize(
    ("values", "which", "expected"),
    [
        ([10, 20, 30, 40], 50, 20),
        ([10, 20, 30, 40], 95, 40),
        ([5], 50, 5),
        ([5], 95, 5),
        ([1] * 19 + [100], 95, 1),
        ([1] * 18 + [99, 100], 95, 99),
    ],
)
def test_percentile_is_nearest_rank(values, which, expected) -> None:
    assert percentile(values, which) == expected


def test_percentile_of_nothing_is_none() -> None:
    assert percentile([], 50) is None


def test_percentiles_are_observed_values() -> None:
    report = summarise(
        "d",
        [
            _document([_field()], document_id=f"doc-{index}", latency=latency)
            for index, latency in enumerate([100, 200, 300, 400, 5000])
        ],
    )

    assert report.p50_latency_ms == 300
    assert report.p95_latency_ms == 5000
    assert report.mean_latency_ms == 1200.0


# --- failures -------------------------------------------------------------


def test_a_failed_document_is_reported_and_its_fields_count_as_wrong() -> None:
    report = summarise(
        "d",
        [
            _document(
                [_field(extracted=None)], error="ProviderError: boom", cost=None,
                latency=None,
            )
        ],
    )

    assert report.failed_documents == ("doc-1",)
    assert report.fields_evaluated == 1
    assert report.overall_accuracy == 0.0


def test_the_model_and_prompt_version_are_carried_through() -> None:
    report = summarise("d", [_document([_field()])])

    assert report.model_name == "fake-model-1"
    assert report.prompt_version == "invoice/v1"


def test_as_dict_is_json_safe() -> None:
    import json

    report = summarise("d", [_document([_field()])])

    payload = json.loads(json.dumps(report.as_dict(), default=str))
    assert payload["dataset"] == "d"
    assert payload["outcomes"]["correct_confident"] == 1


def test_a_failed_extraction_can_never_be_correct() -> None:
    """Not even a null label — the pipeline did not answer "absent", it did
    not answer at all."""
    observation = FieldObservation(
        document_id="doc-1",
        field_name="purchase_order_number",
        expected=None,
        extracted=None,
        needs_review=False,
        confidence=0.0,
        model_confidence=0.0,
        extraction_failed=True,
    )

    assert observation.correct is False
    assert observation.false_confident is True


def test_a_null_label_matched_by_a_real_extraction_is_correct() -> None:
    observation = FieldObservation(
        document_id="doc-1",
        field_name="purchase_order_number",
        expected=None,
        extracted=None,
        needs_review=False,
        confidence=0.9,
        model_confidence=0.9,
    )

    assert observation.correct is True
