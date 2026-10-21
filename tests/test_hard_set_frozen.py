"""`invoices_hard_v1` is frozen: it hashes to the digest recorded in docs/EVAL.md and it is exactly what was reviewed.

If `test_the_hard_set_has_not_moved` fails, a file under evals/datasets/invoices_hard_v1/ changed. If that was
deliberate it is a new dataset version: say why in docs/EVAL.md and record the new digest there.
"""

import json
import re
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from evals import dataset as dataset_module
from evals import runner
from evals.freeze import dataset_digest
from evals.providers import StubProvider

ROOT = Path(__file__).resolve().parent.parent
NAME = "invoices_hard_v1"
DIRECTORY = ROOT / "evals" / "datasets" / NAME
EVAL_MD = (ROOT / "docs" / "EVAL.md").read_text(encoding="utf-8")

EXPECTED_FILES = {
    "FEATURES.json", "README.md", "labels.json",
    "hard-001.png", "hard-002.png", "hard-003.pdf", "hard-004.pdf", "hard-005.jpg",
    "hard-006.pdf", "hard-007.pdf", "hard-008.png", "hard-009.pdf", "hard-010.png",
}


def _recorded_digest() -> str:
    match = re.search(r"^hard-set-sha256: ([0-9a-f]{64})$", EVAL_MD, re.MULTILINE)
    assert match, "docs/EVAL.md does not record a hard-set-sha256"
    return match.group(1)


def test_the_hard_set_has_not_moved() -> None:
    combined, _ = dataset_digest(NAME)

    assert combined == _recorded_digest(), (
        "evals/datasets/invoices_hard_v1 no longer hashes to the frozen digest in docs/EVAL.md; "
        "a change after a model run is a new dataset version"
    )


def test_the_hard_set_contains_exactly_the_reviewed_files() -> None:
    assert {p.name for p in DIRECTORY.iterdir()} == EXPECTED_FILES


def test_the_digest_ignores_line_endings_in_text_files(tmp_path: Path) -> None:
    from evals.freeze import file_digest

    lf, crlf = tmp_path / "a.json", tmp_path / "b.json"
    lf.write_bytes(b'{"a": 1}\n')
    crlf.write_bytes(b'{"a": 1}\r\n')
    binary_lf, binary_crlf = tmp_path / "a.png", tmp_path / "b.png"
    binary_lf.write_bytes(b"\x89PNG\n")
    binary_crlf.write_bytes(b"\x89PNG\r\n")

    assert file_digest(lf) == file_digest(crlf)
    assert file_digest(binary_lf) != file_digest(binary_crlf)  # binary files are hashed as they are


def test_the_labels_are_consistent_and_every_document_is_labelled() -> None:
    loaded = dataset_module.load(NAME)

    assert len(loaded) == 10 and loaded.synthetic
    for document in loaded.documents:
        fields = document.fields
        rows = fields["line_items"]
        assert sum((Decimal(r["amount"]) for r in rows), Decimal(0)) == Decimal(fields["subtotal"]), document.document_id
        assert Decimal(fields["subtotal"]) + Decimal(fields["tax"]) == Decimal(fields["total"]), document.document_id
        for row in rows:
            assert Decimal(row["quantity"]) * Decimal(row["unit_price"]) == Decimal(row["amount"]), document.document_id


def test_the_two_scoring_rules_cases_are_in_the_set_as_decided() -> None:
    labels = {d["document_id"]: d["fields"] for d in json.loads((DIRECTORY / "labels.json").read_text())["documents"]}

    assert all(Decimal(r["amount"]) < 0 for r in labels["hard-004"]["line_items"])  # the credit note
    assert labels["hard-004"]["total"] == "-222.00"
    assert labels["hard-009"]["due_date"] is None  # "Payable on receipt"
    assert labels["hard-009"]["vendor_tax_id"] is None and labels["hard-009"]["purchase_order_number"] is None


def test_the_provenance_and_the_scoring_rules_are_written_down() -> None:
    readme = " ".join((DIRECTORY / "README.md").read_text(encoding="utf-8").split())

    assert "Written by Claude (generated; labels are the render inputs); reviewed by ChatGPT and Claude; final decisions by Sameer." in readme
    for text in (readme, EVAL_MD):
        assert "Credit note" in text or "credit note" in text
        assert "Payable on receipt" in text


def test_the_harness_runs_the_hard_set_end_to_end_on_the_stub(migrated_engine: Engine, storage_dir: Path) -> None:
    """Scans, a photograph and PDFs all go through store, render, extract, validate and score."""
    loaded = dataset_module.load(NAME)

    with Session(migrated_engine) as session:
        report = runner.evaluate(session, loaded, StubProvider())

    assert report.documents == 10
    assert report.failed_documents == ()
