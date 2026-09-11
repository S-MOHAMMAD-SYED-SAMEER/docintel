"""add extractions and field values

Revision ID: 1bcc47c99e5f
Revises: 6f9807e549bb
Create Date: 2026-09-11 06:34:52.791217

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1bcc47c99e5f"
down_revision: str | None = "6f9807e549bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One row per model call, and one row per field that call produced."""
    op.create_table(
        "extractions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column(
            "raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("parsed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_extractions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extractions")),
    )
    op.create_index(
        op.f("ix_extractions_document_id"), "extractions", ["document_id"], unique=False
    )

    op.create_table(
        "field_values",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("field_name", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column(
            "needs_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["extractions.id"],
            name=op.f("fk_field_values_extraction_id_extractions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_values")),
        sa.UniqueConstraint(
            "extraction_id",
            "field_name",
            name=op.f("uq_field_values_extraction_id_field_name"),
        ),
    )
    op.create_index(
        op.f("ix_field_values_extraction_id"),
        "field_values",
        ["extraction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_field_values_extraction_id"), table_name="field_values")
    op.drop_table("field_values")
    op.drop_index(op.f("ix_extractions_document_id"), table_name="extractions")
    op.drop_table("extractions")
