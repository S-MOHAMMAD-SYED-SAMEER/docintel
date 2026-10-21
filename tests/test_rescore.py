"""The offline re-score of the saved real runs: reproducible, and the published figures are the computed ones."""

import json
from pathlib import Path

from evals import rescore

ROOT = Path(__file__).resolve().parent.parent
RESCORED = json.loads((ROOT / "docs" / "results" / "2026-10-21-rescored.json").read_text(encoding="utf-8"))
RUNS = {run["run"]: run for run in RESCORED["runs"]}


def _sum(prompt: str, block: str, key: str) -> int:
    return sum(
        run[block][key]
        for run in RESCORED["runs"]
        if run["prompt_version"] == prompt and run["dataset"].startswith("invoices")
    )


def test_the_file_on_disk_is_what_the_script_writes() -> None:
    """Also proves the old check is recomputed to the saved flags and confidences (the script refuses otherwise)."""
    assert rescore.main(["--check"]) == 0


def test_the_legacy_check_accepted_two_of_the_three_real_misses() -> None:
    """Why only two were unflagged as run: the dropped middle digit was already caught (invoice-010, flagged)."""
    pages = ("VAT FR40123456824",)

    assert rescore.legacy_contains(pages, "FR4012345682")
    assert rescore.legacy_contains(pages, "FR4012345" + chr(0x200B) + "6824")
    assert not rescore.legacy_contains(pages, "FR4012345824")


def test_no_correct_field_was_newly_flagged() -> None:
    for run in RESCORED["runs"]:
        assert run["with_fixed_check"]["correct_flagged"] == run["as_run"]["correct_flagged"], run["run"]


def test_the_two_vat_misses_of_the_first_run_are_now_flagged() -> None:
    changed = RUNS["2026-10-21-invoices_v1-invoice-v1"]["fields_whose_text_layer_verdict_or_flag_changed"]

    assert {(c["filename"], c["field"]) for c in changed} == {
        ("invoice-005.pdf", "vendor_tax_id"),
        ("invoice-015.pdf", "vendor_tax_id"),
    }
    assert all(c["text_layer_fixed"] == "not found" and c["flagged_fixed"] and not c["flagged_as_run"] for c in changed)


def test_the_published_totals() -> None:
    assert _sum("invoice/v1", "as_run", "wrong_confident") == 4
    assert _sum("invoice/v1", "with_fixed_check", "wrong_confident") == 2
    assert _sum("invoice/v2", "as_run", "wrong_confident") == 2
    assert _sum("invoice/v2", "with_fixed_check", "wrong_confident") == 2
    assert _sum("invoice/v1", "as_run", "flagged") == 3
    assert _sum("invoice/v1", "with_fixed_check", "flagged") == 5
    assert _sum("invoice/v2", "as_run", "flagged") == 4
    assert _sum("invoice/v2", "with_fixed_check", "flagged") == 4


def test_readme_and_eval_state_both_versions() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    eval_md = (ROOT / "docs" / "EVAL.md").read_text(encoding="utf-8")
    results = (ROOT / "docs" / "results" / "2026-10-21-real-runs.md").read_text(encoding="utf-8")

    for text in (readme, eval_md, results):
        assert "As run" in text or "as run" in text
        assert "fixed check" in text
    assert "387 of 390 fields correct, 3 wrong, 2 of those not flagged" in readme
    assert "image-only documents" in readme and "0.850" in readme and "Munchen" in readme
