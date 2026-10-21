"""The committed PDFs are byte-exact, on every platform.

The demo recognises a sample invoice by the SHA-256 of its source PDF's bytes (`demo/fixtures/`). Git for
Windows with `core.autocrlf=true` rewrites LF to CRLF in files it takes for text, which changes those bytes:
the fixture hashes were once computed from such a mangled checkout, so the demo recognised nothing on Linux or
in Docker. `.gitattributes` now marks PDFs and images as binary; these tests fail if a checkout ever mangles
them again, or if a fixture hash no longer matches its PDF.
"""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "evals" / "datasets"
FIXTURES = ROOT / "demo" / "fixtures" / "invoice_answers.json"

PDFS = sorted(DATASETS.glob("*/*.pdf"))


def test_there_are_pdfs_to_check() -> None:
    assert len(PDFS) == 28  # 20 + 3 + the 5 PDFs of invoices_hard_v1


@pytest.mark.parametrize("pdf", PDFS, ids=lambda path: path.name)
def test_a_committed_pdf_has_no_carriage_returns_added_by_a_checkout(pdf: Path) -> None:
    # The generator writes LF only, so a CR-LF pair means a checkout rewrote the file.
    assert b"\r\n" not in pdf.read_bytes(), f"{pdf.name} was line-ending converted by the checkout"


def test_every_demo_fixture_hash_matches_its_committed_pdf() -> None:
    fixtures = json.loads(FIXTURES.read_text())
    checked = 0
    for fixture_id, entry in fixtures.items():
        if fixture_id.startswith("_"):
            continue
        digest = hashlib.sha256((ROOT / entry["source_pdf"]).read_bytes()).hexdigest()
        assert entry["source_sha256"] == digest, fixture_id
        checked += 1
    assert checked == 3


def test_gitattributes_marks_pdfs_as_binary() -> None:
    attributes = (ROOT / ".gitattributes").read_text()
    assert "*.pdf binary" in attributes
