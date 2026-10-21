"""What the README, docs/EVAL.md and the results page say about the real-model runs of 2026-10-21 is what the committed records say.

A figure typed into prose can drift from the run it came from. These tests read the committed JSON records and check the
numbers the documents print, and that the limits are printed beside them.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "results"
README = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
EVAL = " ".join((ROOT / "docs" / "EVAL.md").read_text(encoding="utf-8").split())
PAGE = " ".join((RESULTS / "2026-10-21-real-runs.md").read_text(encoding="utf-8").split())

RUNS = {
    ("invoices_v1", "invoice/v1"): "invoices_v1-invoice-v1",
    ("invoices_v1", "invoice/v2"): "invoices_v1-invoice-v2",
    ("invoices_hard_v1", "invoice/v1"): "invoices_hard_v1-invoice-v1",
    ("invoices_hard_v1", "invoice/v2"): "invoices_hard_v1-invoice-v2",
}


def _load(name: str) -> dict:
    return json.loads((RESULTS / f"2026-10-21-{name}.json").read_text(encoding="utf-8"))


def _counts(run: dict) -> tuple[int, int, int, int]:
    fields = [f for d in run["documents"] for f in d["fields"].values()]
    wrong = [f for f in fields if not f["correct"]]
    return len(fields), len(fields) - len(wrong), sum(1 for f in wrong if not f["needs_review"]), sum(1 for f in fields if f["needs_review"])


@pytest.mark.parametrize(("dataset", "prompt"), sorted(RUNS))
def test_a_record_is_what_the_run_claims_to_be(dataset: str, prompt: str) -> None:
    run = _load(RUNS[(dataset, prompt)])

    assert run["meta"]["dataset"] == dataset and run["meta"]["prompt_version"] == prompt
    assert run["meta"]["synthetic"] is True and run["meta"]["adapter_retries"] == 0
    assert run["meta"]["model_resolved_by_the_api"] == "claude-haiku-4-5-20251001"
    assert run["report"]["failed_documents"] == []
    assert all(d["error"] is None and d["prompt_version"] == prompt for d in run["documents"])
    total, correct, _, _ = _counts(run)
    assert total == run["report"]["fields_evaluated"]
    assert correct / total == pytest.approx(run["report"]["overall_accuracy"])


def test_the_hard_set_record_carries_the_frozen_digest() -> None:
    recorded = next(line for line in (ROOT / "docs" / "EVAL.md").read_text(encoding="utf-8").splitlines() if line.startswith("hard-set-sha256: "))
    run = _load(RUNS[("invoices_hard_v1", "invoice/v1")])

    assert run["meta"]["dataset_sha256"] == recorded.split(": ")[1]
    assert "reviewed by ChatGPT and Claude" in run["meta"]["provenance"]


def test_the_readme_the_eval_plan_and_the_page_quote_the_committed_counts() -> None:
    inv1, inv2 = _counts(_load(RUNS[("invoices_v1", "invoice/v1")])), _counts(_load(RUNS[("invoices_v1", "invoice/v2")]))
    hard1, hard2 = _counts(_load(RUNS[("invoices_hard_v1", "invoice/v1")])), _counts(_load(RUNS[("invoices_hard_v1", "invoice/v2")]))

    for text in (README, PAGE):
        for total, correct, _, _ in (inv1, inv2, hard1, hard2):
            assert f"{correct} of {total}" in text
    v1_wrong = (inv1[0] - inv1[1]) + (hard1[0] - hard1[1])
    v2_wrong = (inv2[0] - inv2[1]) + (hard2[0] - hard2[1])
    v1_fc, v2_fc = inv1[2] + hard1[2], inv2[2] + hard2[2]
    v1_flagged, v2_flagged = inv1[3] + hard1[3], inv2[3] + hard2[3]
    assert (v1_wrong, v2_wrong, v1_fc, v2_fc, v1_flagged, v2_flagged) == (6, 3, 4, 2, 3, 4)
    for text in (README, EVAL, PAGE):
        assert f"{v1_wrong} of 390" in text or f"| {v1_wrong} | {v2_wrong} |" in text or f"Wrong fields | {v1_wrong}" in text
    assert "384 of 390" in EVAL and "387 of 390" in EVAL


def test_the_ledger_numbers_are_the_ledger() -> None:
    ledger = json.loads((RESULTS / "2026-10-21-ledger.json").read_text(encoding="utf-8"))

    assert ledger["requests_used"] == len(ledger["entries"]) == 64 and ledger["request_cap"] == 80
    assert Decimal(ledger["cost_usd_total"]) == sum((Decimal(e["cost_usd"]) for e in ledger["entries"]), Decimal(0))
    assert Decimal(ledger["cost_usd_total"]) <= Decimal(ledger["cost_cap_usd"])
    for text in (EVAL, PAGE):
        assert "64 of 80" in text and f"${ledger['cost_usd_total']}" in text


@pytest.mark.parametrize("text", [README, PAGE, EVAL], ids=["README", "results page", "EVAL.md"])
def test_the_limits_are_printed_beside_the_numbers(text: str) -> None:
    lowered = text.lower()

    assert "synthetic" in lowered
    assert "one run" in lowered
    assert "by construction" in lowered
    assert "not an accuracy figure" in lowered or "not a claim about real invoices" in lowered or "no claim about real invoices" in lowered


def test_nothing_claims_accuracy_or_production_readiness() -> None:
    for text in (README, PAGE, EVAL):
        lowered = text.lower()
        assert "production-ready" not in lowered.replace("production readiness", "")
        assert "no real-model benchmark has been run" not in lowered
