"""The documents table, as created by the migration on a fresh database."""

import uuid

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.models import Document, DocumentStatus

README_COLUMNS = {
    "id",
    "filename",
    "storage_path",
    "doc_type",
    "page_count",
    "status",
    "uploaded_at",
}
# Beyond the README data model: why a document is in FAILED (milestone 3).
EXTRA_COLUMNS = {"error"}
ALL_COLUMNS = README_COLUMNS | EXTRA_COLUMNS
README_STATUSES = [
    "uploaded",
    "processing",
    "extracted",
    "needs_review",
    "reviewed",
    "failed",
]


def test_migration_creates_the_documents_table(migrated_engine: Engine) -> None:
    assert "documents" in inspect(migrated_engine).get_table_names()


def test_columns_match_the_readme_data_model(migrated_engine: Engine) -> None:
    columns = inspect(migrated_engine).get_columns("documents")
    assert {column["name"] for column in columns} == ALL_COLUMNS

    by_name = {column["name"]: column for column in columns}
    assert by_name["id"]["type"].python_type is uuid.UUID
    # page_count is unknown until pages are rendered and error is only set on
    # failure; every column the README names is otherwise required.
    assert by_name["page_count"]["nullable"] is True
    assert by_name["error"]["nullable"] is True
    assert not any(
        by_name[name]["nullable"] for name in README_COLUMNS - {"page_count"}
    )


def test_primary_key_follows_the_naming_convention(migrated_engine: Engine) -> None:
    pk = inspect(migrated_engine).get_pk_constraint("documents")
    assert pk["constrained_columns"] == ["id"]
    assert pk["name"] == "pk_documents"


def test_status_enum_has_exactly_the_readme_values(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        labels = connection.execute(
            text(
                "select enumlabel from pg_enum e "
                "join pg_type t on t.oid = e.enumtypid "
                "where t.typname = 'document_status' order by e.enumsortorder"
            )
        ).scalars().all()

    assert labels == README_STATUSES
    assert [status.value for status in DocumentStatus] == README_STATUSES


def test_insert_defaults_id_status_and_uploaded_at(session: Session) -> None:
    document = Document(
        filename="invoice-001.pdf",
        storage_path="var/storage/invoice-001.pdf",
        doc_type="invoice",
    )
    session.add(document)
    session.commit()

    assert isinstance(document.id, uuid.UUID)
    assert document.status is DocumentStatus.UPLOADED
    assert document.uploaded_at is not None
    assert document.page_count is None


def test_status_round_trips_as_an_enum(session: Session) -> None:
    document = Document(
        filename="po-7.pdf",
        storage_path="var/storage/po-7.pdf",
        doc_type="purchase_order",
        page_count=3,
        status=DocumentStatus.NEEDS_REVIEW,
    )
    session.add(document)
    session.commit()
    document_id = document.id
    session.expunge_all()

    stored = session.get(Document, document_id)
    assert stored is not None
    assert stored.status is DocumentStatus.NEEDS_REVIEW
    assert stored.page_count == 3


def test_database_rejects_a_status_outside_the_enum(session: Session) -> None:
    with pytest.raises(DBAPIError):
        session.execute(
            text(
                "insert into documents (filename, storage_path, doc_type, status) "
                "values ('x.pdf', 'var/storage/x.pdf', 'invoice', 'shredded')"
            )
        )
    session.rollback()


def test_database_rejects_a_missing_filename(session: Session) -> None:
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "insert into documents (storage_path, doc_type) "
                "values ('var/storage/x.pdf', 'invoice')"
            )
        )
    session.rollback()
