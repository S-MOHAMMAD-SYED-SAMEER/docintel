"""Loading the labelled dataset."""

import json
from pathlib import Path

import pytest

from evals import dataset as dataset_module
from evals.dataset import DatasetError

DATASET_NAME = "invoices_v1"
EXPECTED_FIELDS = {
    "invoice_number",
    "invoice_date",
    "due_date",
    "vendor_name",
    "vendor_address",
    "vendor_tax_id",
    "customer_name",
    "purchase_order_number",
    "currency",
    "subtotal",
    "tax",
    "total",
    "line_items",
}


@pytest.fixture
def loaded():
    return dataset_module.load(DATASET_NAME)


def test_the_dataset_is_listed() -> None:
    assert DATASET_NAME in dataset_module.available()


def test_the_dataset_has_exactly_twenty_documents(loaded) -> None:
    assert len(loaded) == 20
    assert len(loaded.documents) == 20


def test_every_labelled_file_exists_on_disk(loaded) -> None:
    for document in loaded.documents:
        assert document.path.is_file()
        assert document.path.read_bytes().startswith(b"%PDF-")


def test_document_ids_are_unique(loaded) -> None:
    ids = [document.document_id for document in loaded.documents]

    assert len(set(ids)) == len(ids)


def test_the_dataset_declares_itself_synthetic(loaded) -> None:
    """No real invoice is included, and the labels say so."""
    assert loaded.synthetic is True
    assert "synthetic" in loaded.description.lower()
    assert "not be quoted as real-world accuracy" in loaded.description


def test_every_document_labels_every_field(loaded) -> None:
    for document in loaded.documents:
        assert set(document.fields) == EXPECTED_FIELDS, document.document_id


def test_labels_cover_the_extractor_schema(loaded) -> None:
    from app.extractors import Invoice

    assert EXPECTED_FIELDS == set(Invoice.model_fields)


def test_line_item_labels_are_structured(loaded) -> None:
    rows = loaded.documents[0].fields["line_items"]

    assert isinstance(rows, list)
    assert rows
    assert set(rows[0]) == {"description", "quantity", "unit_price", "amount"}


def test_the_labels_are_arithmetically_consistent(loaded) -> None:
    """If the labels did not add up, the arithmetic checks would be meaningless."""
    from decimal import Decimal

    for document in loaded.documents:
        fields = document.fields
        rows = sum(
            (Decimal(row["amount"]) for row in fields["line_items"]), Decimal(0)
        )
        assert rows == Decimal(fields["subtotal"]), document.document_id
        assert Decimal(fields["subtotal"]) + Decimal(fields["tax"]) == Decimal(
            fields["total"]
        ), document.document_id


def test_the_pdf_text_matches_the_labels(loaded) -> None:
    """The labels describe what the document says, not what a model says."""
    import pypdfium2 as pdfium

    document = loaded.documents[3]
    pdf = pdfium.PdfDocument(document.path)
    text = "\n".join(
        pdf[index].get_textpage().get_text_range() for index in range(len(pdf))
    )
    pdf.close()

    for field_name in ("invoice_number", "vendor_name", "total", "currency"):
        assert str(document.fields[field_name]) in text, field_name


# --- failure modes --------------------------------------------------------


def test_an_unknown_dataset_fails_clearly() -> None:
    with pytest.raises(DatasetError, match="No dataset named 'nope'"):
        dataset_module.load("nope")


def test_a_directory_without_labels_fails_clearly(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    with pytest.raises(DatasetError, match="labels.json is missing"):
        dataset_module.load("empty", datasets_dir=tmp_path)


def _write(tmp_path: Path, payload: object) -> Path:
    directory = tmp_path / "broken"
    directory.mkdir(exist_ok=True)
    (directory / "labels.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload)
    )
    return tmp_path


def test_malformed_json_fails_clearly(tmp_path: Path) -> None:
    root = _write(tmp_path, "{not json")

    with pytest.raises(DatasetError, match="not valid JSON"):
        dataset_module.load("broken", datasets_dir=root)


def test_labels_without_documents_fail_clearly(tmp_path: Path) -> None:
    root = _write(tmp_path, {"dataset": "broken"})

    with pytest.raises(DatasetError, match="no 'documents' list"):
        dataset_module.load("broken", datasets_dir=root)


def test_a_document_missing_a_key_fails_clearly(tmp_path: Path) -> None:
    root = _write(tmp_path, {"documents": [{"document_id": "a", "fields": {"x": 1}}]})

    with pytest.raises(DatasetError, match="missing 'filename'"):
        dataset_module.load("broken", datasets_dir=root)


def test_a_document_with_empty_fields_fails_clearly(tmp_path: Path) -> None:
    root = _write(
        tmp_path, {"documents": [{"document_id": "a", "filename": "a.pdf", "fields": {}}]}
    )

    with pytest.raises(DatasetError, match="non-empty object"):
        dataset_module.load("broken", datasets_dir=root)


def test_a_labelled_file_that_is_not_present_fails_clearly(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {"documents": [{"document_id": "a", "filename": "gone.pdf", "fields": {"x": 1}}]},
    )

    with pytest.raises(DatasetError, match="labelled but not present"):
        dataset_module.load("broken", datasets_dir=root)


def test_duplicate_document_ids_fail_clearly(tmp_path: Path) -> None:
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "a.pdf").write_bytes(b"%PDF-1.4")
    (directory / "labels.json").write_text(
        json.dumps(
            {
                "documents": [
                    {"document_id": "a", "filename": "a.pdf", "fields": {"x": 1}},
                    {"document_id": "a", "filename": "a.pdf", "fields": {"x": 2}},
                ]
            }
        )
    )

    with pytest.raises(DatasetError, match="duplicate document_id"):
        dataset_module.load("broken", datasets_dir=tmp_path)
