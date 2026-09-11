"""add eval runs

Revision ID: 7de8d00eccf5
Revises: fec54a40bb19
Create Date: 2026-09-11 07:30:44.612905

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7de8d00eccf5"
down_revision: str | None = "fec54a40bb19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One row per evaluation of a labelled dataset."""
    op.create_table(
        "eval_runs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("dataset_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column(
            "field_accuracy", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("review_rate", sa.Float(), nullable=True),
        sa.Column("false_confident_rate", sa.Float(), nullable=True),
        sa.Column("mean_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("mean_latency_ms", sa.Float(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_runs")),
    )
    op.create_index(
        op.f("ix_eval_runs_dataset_name"), "eval_runs", ["dataset_name"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_eval_runs_dataset_name"), table_name="eval_runs")
    op.drop_table("eval_runs")
