"""The runner, persistence and the CLI — all against a fake provider."""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import EvalRun
from evals import dataset as dataset_module
from evals import providers, runner
from evals.metrics import Report
from evals.providers import StubProvider
from evals.report import render

from .conftest import VALID_INVOICE_PAYLOAD


class LabelEchoProvider:
    """Returns each document's labels, so the pipeline is exercised at 100%.

    Used only to prove the harness reports a perfect score when extraction is
    perfect. The real evaluator never sees labels — this provider is handed
    them explicitly by the test, and is not available to the CLI.
    """

    model_name = "label-echo"

    def __init__(self, dataset, *, corrupt: dict[str, object] | None = None) -> None:
        # field name -> the wrong value to return instead of the label.
        self._corrupt = corrupt or {}
        self._remaining = list(dataset.documents)

    def extract(self, images, schema, prompt):
        from app.providers import RawExtraction

        labelled = self._remaining.pop(0)
        payload = {}
        for name, value in labelled.fields.items():
            if name in self._corrupt:
                value = self._corrupt[name]
            payload[name] = {"value": value, "confidence": 0.99, "source_page": 1}

        content = json.dumps(payload)
        return RawExtraction(
            content=content,
            raw_response={
                "id": "echo",
                "model": self.model_name,
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            },
            model_name=self.model_name,
            input_tokens=1000,
            output_tokens=200,
            cost_usd=__import__("decimal").Decimal("0.004000"),
            latency_ms=1500,
        )


@pytest.fixture
def small_dataset():
    """The first three documents of the real dataset."""
    full = dataset_module.load("invoices_v1")
    return dataset_module.Dataset(
        name=full.name,
        directory=full.directory,
        synthetic=full.synthetic,
        description=full.description,
        documents=full.documents[:3],
    )


@pytest.fixture
def eval_session(migrated_engine: Engine, storage_dir: Path):
    with Session(migrated_engine) as session:
        yield session


# --- the runner -----------------------------------------------------------


def test_a_perfect_extraction_scores_a_perfect_report(
    eval_session, small_dataset
) -> None:
    report = runner.evaluate(
        eval_session, small_dataset, LabelEchoProvider(small_dataset)
    )

    assert report.documents == 3
    assert report.fields_evaluated == 39
    assert report.overall_accuracy == 1.0
    assert report.false_confident_rate == 0.0
    assert report.failed_documents == ()


def test_a_wrong_field_is_counted_wrong(eval_session, small_dataset) -> None:
    report = runner.evaluate(
        eval_session,
        small_dataset,
        LabelEchoProvider(small_dataset, corrupt={"invoice_number": "INV-0000-0000"}),
    )

    assert report.field_accuracy["invoice_number"].accuracy == 0.0
    assert report.field_accuracy["vendor_name"].accuracy == 1.0
    assert report.overall_accuracy < 1.0


def test_a_wrong_total_is_caught_by_the_arithmetic_check(
    eval_session, small_dataset
) -> None:
    """A type-valid but wrong total breaks subtotal + tax = total, so it is
    flagged by the deterministic layer and counted as caught, not as
    false-confident."""
    report = runner.evaluate(
        eval_session,
        small_dataset,
        LabelEchoProvider(small_dataset, corrupt={"total": "9999.99"}),
    )

    assert report.field_accuracy["total"].accuracy == 0.0
    assert report.wrong_flagged >= 3
    assert report.false_confident_rate < 1.0


def test_the_stub_provider_never_reports_cost_or_latency(
    eval_session, small_dataset
) -> None:
    """A stub must not invent numbers it did not measure."""
    report = runner.evaluate(eval_session, small_dataset, StubProvider())

    assert report.mean_cost_usd is None
    assert report.mean_latency_ms is None
    assert report.documents_with_cost == 0


def test_cost_and_latency_come_from_the_extraction_rows(
    eval_session, small_dataset
) -> None:
    report = runner.evaluate(
        eval_session, small_dataset, LabelEchoProvider(small_dataset)
    )

    assert report.documents_with_cost == 3
    assert report.mean_cost_usd is not None
    assert report.mean_latency_ms == 1500.0
    assert report.p50_latency_ms == 1500


def test_a_provider_failure_is_reported_not_skipped(
    eval_session, small_dataset
) -> None:
    from app.providers import ProviderError

    class BrokenProvider:
        model_name = "broken"

        def extract(self, images, schema, prompt):
            raise ProviderError("the api fell over")

    report = runner.evaluate(eval_session, small_dataset, BrokenProvider())

    assert len(report.failed_documents) == 3
    # Every labelled field still counted, as wrong.
    assert report.fields_evaluated == 39
    assert report.overall_accuracy == 0.0


def test_an_unparseable_answer_is_a_failure(eval_session, small_dataset) -> None:
    class GibberishProvider:
        model_name = "gibberish"

        def extract(self, images, schema, prompt):
            from app.providers import RawExtraction

            return RawExtraction(
                content="not json",
                raw_response={"id": "x", "content": []},
                model_name=self.model_name,
            )

    report = runner.evaluate(eval_session, small_dataset, GibberishProvider())

    assert len(report.failed_documents) == 3
    assert report.overall_accuracy == 0.0


def test_the_runner_uses_the_real_pipeline(eval_session, small_dataset) -> None:
    """Documents, extractions and scored field values are all really written."""
    from app.models import Document, Extraction, FieldValue

    runner.evaluate(eval_session, small_dataset, LabelEchoProvider(small_dataset))

    assert eval_session.execute(select(Document)).scalars().all().__len__() == 3
    assert eval_session.execute(select(Extraction)).scalars().all().__len__() == 3
    assert len(eval_session.execute(select(FieldValue)).scalars().all()) == 39


# --- persistence ----------------------------------------------------------


def test_persist_writes_an_eval_run(eval_session, small_dataset) -> None:
    report = runner.evaluate(
        eval_session, small_dataset, LabelEchoProvider(small_dataset)
    )

    run = runner.persist(
        eval_session, report, model_name="label-echo", prompt_version="invoice/v1"
    )

    assert isinstance(run.id, uuid.UUID)
    assert run.dataset_name == "invoices_v1"
    assert run.prompt_version == "invoice/v1"
    assert run.model_name == "label-echo"
    assert run.document_count == 3
    assert run.created_at is not None


def test_persisted_field_accuracy_is_per_field(eval_session, small_dataset) -> None:
    report = runner.evaluate(
        eval_session,
        small_dataset,
        LabelEchoProvider(small_dataset, corrupt={"invoice_number": "INV-0000-0000"}),
    )

    run = runner.persist(
        eval_session, report, model_name="label-echo", prompt_version="invoice/v1"
    )

    assert run.field_accuracy["invoice_number"]["accuracy"] == 0.0
    assert run.field_accuracy["vendor_name"]["accuracy"] == 1.0
    assert run.field_accuracy["total"]["evaluated"] == 3


def test_persisted_rates_cost_and_latency(eval_session, small_dataset) -> None:
    report = runner.evaluate(
        eval_session, small_dataset, LabelEchoProvider(small_dataset)
    )

    run = runner.persist(
        eval_session, report, model_name="label-echo", prompt_version="invoice/v1"
    )

    assert run.review_rate == pytest.approx(report.review_rate)
    assert run.false_confident_rate == pytest.approx(report.false_confident_rate)
    assert run.mean_cost_usd == report.mean_cost_usd
    assert run.mean_latency_ms == 1500.0


def test_the_metrics_blob_holds_the_percentiles_and_outcomes(
    eval_session, small_dataset
) -> None:
    report = runner.evaluate(
        eval_session, small_dataset, LabelEchoProvider(small_dataset)
    )

    run = runner.persist(
        eval_session, report, model_name="label-echo", prompt_version="invoice/v1"
    )

    assert run.metrics["latency_ms"]["p95"] == 1500
    assert set(run.metrics["outcomes"]) == {
        "correct_confident",
        "correct_flagged",
        "wrong_flagged",
        "wrong_confident",
    }
    assert run.metrics["failed_documents"] == []


def test_repeated_runs_create_separate_records(eval_session, small_dataset) -> None:
    for _ in range(2):
        report = runner.evaluate(
            eval_session, small_dataset, LabelEchoProvider(small_dataset)
        )
        runner.persist(
            eval_session, report, model_name="label-echo", prompt_version="invoice/v1"
        )

    runs = eval_session.execute(select(EvalRun)).scalars().all()
    assert len(runs) == 2
    assert len({run.id for run in runs}) == 2


# --- report rendering -----------------------------------------------------


def _rendered(eval_session, dataset, provider) -> str:
    report = runner.evaluate(eval_session, dataset, provider)
    return render(report, threshold=0.85, notes=["a note"])


def test_the_report_contains_every_required_metric(
    eval_session, small_dataset
) -> None:
    text = _rendered(eval_session, small_dataset, LabelEchoProvider(small_dataset))

    for expected in (
        "invoices_v1",
        "label-echo",
        "invoice/v1",
        "Exact-match accuracy per field",
        "invoice_number",
        "review rate",
        "FALSE-CONFIDENT RATE",
        "mean per document",
        "mean  ",
        "p50",
        "p95",
        "a note",
    ):
        assert expected in text, expected


def test_the_report_says_when_cost_is_unavailable(
    eval_session, small_dataset
) -> None:
    text = _rendered(eval_session, small_dataset, StubProvider())

    assert "not available" in text


def test_the_report_lists_failures(eval_session, small_dataset) -> None:
    class BrokenProvider:
        model_name = "broken"

        def extract(self, images, schema, prompt):
            from app.providers import ProviderError

            raise ProviderError("boom")

    text = _rendered(eval_session, small_dataset, BrokenProvider())

    assert "FAILURES (3)" in text
    assert "counted as wrong, not skipped" in text


def test_an_empty_report_renders() -> None:
    from evals.metrics import summarise

    text = render(summarise("empty", []), threshold=0.85)

    assert "n/a" in text
    assert "No document failed to extract." in text


# --- provider selection ---------------------------------------------------


def test_the_stub_provider_is_offline_and_label_blind() -> None:
    provider = StubProvider()

    first = provider.extract([], object, "prompt")
    second = provider.extract([], object, "prompt")

    assert first.content == second.content
    assert first.cost_usd is None and first.latency_ms is None


def test_build_rejects_an_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        providers.build("magic")


def test_anthropic_is_the_default_cli_provider() -> None:
    """A real evaluation costs money, and that has to be the explicit default."""
    from evals.run import build_parser

    args = build_parser().parse_args(["--dataset", "invoices_v1"])

    assert args.provider == providers.ANTHROPIC
