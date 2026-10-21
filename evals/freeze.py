"""A digest of a dataset directory, so a frozen dataset can be checked.

`python -m evals.freeze invoices_hard_v1` prints one sha256 over every file in the directory: for each file, in
sorted path order, the name and the sha256 of its bytes. Text files (`.json`, `.md`) are hashed with line endings
normalised to LF, so a checkout that converts them (Git for Windows with `core.autocrlf=true`) does not change the
digest; images and PDFs are hashed as they are (`.gitattributes` marks them binary).

The digest is recorded in `docs/EVAL.md` (`hard-set-sha256:`), and `tests/test_hard_set_frozen.py` fails if any
file of the dataset differs from it. Changing a frozen dataset is a new dataset version, not an edit.
"""

import hashlib
import sys
from pathlib import Path

DATASETS = Path(__file__).resolve().parent / "datasets"
TEXT_SUFFIXES = {".json", ".md"}


def file_digest(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def dataset_files(directory: Path) -> list[Path]:
    return sorted((p for p in directory.rglob("*") if p.is_file()), key=lambda p: p.relative_to(directory).as_posix())


def dataset_digest(name: str, datasets_dir: Path | None = None) -> tuple[str, dict[str, str]]:
    """The combined digest and each file's own digest."""
    directory = (datasets_dir or DATASETS) / name
    per_file = {p.relative_to(directory).as_posix(): file_digest(p) for p in dataset_files(directory)}
    combined = hashlib.sha256()
    for relative, digest in per_file.items():
        combined.update(f"{relative}\0{digest}\n".encode())
    return combined.hexdigest(), per_file


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m evals.freeze <dataset-name>", file=sys.stderr)
        return 2
    combined, per_file = dataset_digest(args[0])
    for relative, digest in per_file.items():
        print(f"{digest}  {relative}")
    print(f"\ndataset-sha256: {combined}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
