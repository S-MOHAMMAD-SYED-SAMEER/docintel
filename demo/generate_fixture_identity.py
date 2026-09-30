"""Regenerate `demo/fixtures/invoice_answers.json`'s `source_sha256` values.

Fixture *identity* (which known sample document this is) is the SHA-256 of
the committed source PDF's own bytes -- never of a rendered page image,
never of a filename, never of a document id. Source bytes are identical on
every OS, every architecture, and every pypdfium2/Pillow version, so this
hash needs computing exactly once, ever, and never again unless the source
PDF itself changes.

Run:

    python -m demo.generate_fixture_identity

Reads `demo/fixtures/invoice_answers.json`, and for every entry, hashes the
committed PDF named by that entry's own `source_pdf` field. Writes each
entry's `source_sha256` back in place. Every other field -- the answer, the
confidence values, the schema name, notes -- is left byte-for-byte
untouched: this script only ever touches the one key it is responsible for.

No network access, no Anthropic, no PDF rendering. Deterministic: running
it twice in a row produces an identical file both times.

Fails loudly, before writing anything, if any entry's `source_pdf` does not
exist on disk -- a missing fixture source is a broken repository state, not
something to skip past silently.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_FILE = REPO_ROOT / "demo" / "fixtures" / "invoice_answers.json"


class FixtureIdentityError(RuntimeError):
    """The fixture file or one of its declared source PDFs is not usable."""


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_source_hashes(fixture_file: Path = FIXTURE_FILE) -> dict[str, str]:
    """`{fixture_id: source_sha256}` for every entry in the fixture file.

    Raises `FixtureIdentityError` before returning anything if any entry is
    missing `source_pdf` or that path does not exist -- never a partial
    result.
    """
    try:
        raw = fixture_file.read_text()
    except OSError as exc:
        raise FixtureIdentityError(f"Cannot read {fixture_file}: {exc}") from exc

    data = json.loads(raw)

    hashes: dict[str, str] = {}
    for fixture_id, entry in data.items():
        if fixture_id.startswith("_"):
            continue
        source_pdf = entry.get("source_pdf")
        if not source_pdf:
            raise FixtureIdentityError(
                f"Fixture {fixture_id!r} has no 'source_pdf' to hash."
            )
        pdf_path = REPO_ROOT / source_pdf
        if not pdf_path.is_file():
            raise FixtureIdentityError(
                f"Fixture {fixture_id!r} names source_pdf {source_pdf!r}, "
                f"which does not exist at {pdf_path}."
            )
        hashes[fixture_id] = _sha256_of(pdf_path)

    if not hashes:
        raise FixtureIdentityError(f"{fixture_file} has no usable entries.")

    return hashes


def apply(fixture_file: Path = FIXTURE_FILE) -> dict[str, str]:
    """Recompute every entry's `source_sha256` and write the file in place.

    Only ever sets the `source_sha256` key on each entry; nothing else in
    the file is read, reordered, or rewritten beyond what `json.dump`'s own
    formatting produces. Uses `object_pairs_hook` so key order (and the
    file's `_comment` entry) is preserved exactly as written.
    """
    hashes = compute_source_hashes(fixture_file)

    from collections import OrderedDict

    data = json.loads(fixture_file.read_text(), object_pairs_hook=OrderedDict)
    for fixture_id, digest in hashes.items():
        data[fixture_id]["source_sha256"] = digest

    fixture_file.write_text(json.dumps(data, indent=2) + "\n")
    return hashes


def _main() -> None:
    hashes = apply()
    for fixture_id, digest in hashes.items():
        print(f"{fixture_id}: {digest}")
    print(f"wrote source_sha256 for {len(hashes)} fixture(s) to {FIXTURE_FILE}")


if __name__ == "__main__":
    _main()


__all__ = ["FixtureIdentityError", "apply", "compute_source_hashes"]
