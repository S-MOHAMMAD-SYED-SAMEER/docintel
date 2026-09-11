"""Loading a labelled dataset from disk.

Labels are authoritative and live beside the documents. Nothing here ever sees
an extraction — the comparison happens in `evals.metrics`, against these values.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
LABELS_FILENAME = "labels.json"
# A dataset declares the document type it exercises. Datasets written before
# the field existed are invoices, so that is the fallback.
DEFAULT_DOC_TYPE = "invoice"


class DatasetError(Exception):
    """The dataset is missing or its labels cannot be trusted."""


@dataclass(frozen=True)
class LabelledDocument:
    document_id: str
    filename: str
    path: Path
    # field name -> expected value, exactly as the document states it.
    fields: dict[str, Any]


@dataclass(frozen=True)
class Dataset:
    name: str
    directory: Path
    synthetic: bool
    description: str
    documents: tuple[LabelledDocument, ...]
    # Which extractor, validator and prompt this dataset exercises.
    doc_type: str = DEFAULT_DOC_TYPE

    def __len__(self) -> int:
        return len(self.documents)

    @property
    def field_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for document in self.documents:
            for field_name in document.fields:
                if field_name not in names:
                    names.append(field_name)
        return tuple(names)


def available(datasets_dir: Path | None = None) -> tuple[str, ...]:
    root = datasets_dir or DATASETS_DIR
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            entry.name
            for entry in root.iterdir()
            if (entry / LABELS_FILENAME).is_file()
        )
    )


def load(name: str, datasets_dir: Path | None = None) -> Dataset:
    """Load a dataset, failing loudly on anything that would skew a metric."""
    root = datasets_dir or DATASETS_DIR
    directory = root / name

    if not directory.is_dir():
        known = ", ".join(available(root)) or "none"
        raise DatasetError(f"No dataset named {name!r}. Available: {known}.")

    labels_path = directory / LABELS_FILENAME
    if not labels_path.is_file():
        raise DatasetError(f"{name}: {LABELS_FILENAME} is missing.")

    try:
        payload = json.loads(labels_path.read_text())
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{name}: {LABELS_FILENAME} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise DatasetError(f"{name}: {LABELS_FILENAME} must be a JSON object.")

    entries = payload.get("documents")
    if not isinstance(entries, list) or not entries:
        raise DatasetError(f"{name}: {LABELS_FILENAME} has no 'documents' list.")

    documents = tuple(
        _labelled_document(name, directory, index, entry)
        for index, entry in enumerate(entries)
    )

    seen: set[str] = set()
    for document in documents:
        if document.document_id in seen:
            raise DatasetError(
                f"{name}: duplicate document_id {document.document_id!r}."
            )
        seen.add(document.document_id)

    return Dataset(
        name=payload.get("dataset", name),
        directory=directory,
        synthetic=bool(payload.get("synthetic", False)),
        description=str(payload.get("description", "")),
        documents=documents,
        doc_type=str(payload.get("doc_type", DEFAULT_DOC_TYPE)),
    )


def _labelled_document(
    name: str, directory: Path, index: int, entry: Any
) -> LabelledDocument:
    where = f"{name}: documents[{index}]"
    if not isinstance(entry, dict):
        raise DatasetError(f"{where} must be an object.")

    for key in ("document_id", "filename", "fields"):
        if key not in entry:
            raise DatasetError(f"{where} is missing {key!r}.")

    fields = entry["fields"]
    if not isinstance(fields, dict) or not fields:
        raise DatasetError(f"{where}: 'fields' must be a non-empty object.")

    path = directory / str(entry["filename"])
    if not path.is_file():
        raise DatasetError(
            f"{where}: {entry['filename']} is labelled but not present in {directory}."
        )

    return LabelledDocument(
        document_id=str(entry["document_id"]),
        filename=str(entry["filename"]),
        path=path,
        fields=dict(fields),
    )
