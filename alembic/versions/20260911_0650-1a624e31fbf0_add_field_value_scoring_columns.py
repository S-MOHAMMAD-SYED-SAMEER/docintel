"""add field value scoring columns

Revision ID: 1a624e31fbf0
Revises: 1bcc47c99e5f
Create Date: 2026-09-11 06:50:11.204413

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1a624e31fbf0"
down_revision: str | None = "1bcc47c99e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Separate the model's own confidence from the final score.

    Up to now `confidence` held the model's number; from here it holds the
    combined score. Existing rows are backfilled from `confidence`, which is
    exactly what their model confidence was, before the column is made
    non-nullable.
    """
    op.add_column(
        "field_values", sa.Column("model_confidence", sa.Float(), nullable=True)
    )
    op.execute("UPDATE field_values SET model_confidence = confidence")
    op.alter_column("field_values", "model_confidence", nullable=False)

    op.add_column(
        "field_values",
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("field_values", "validation")
    op.drop_column("field_values", "model_confidence")
