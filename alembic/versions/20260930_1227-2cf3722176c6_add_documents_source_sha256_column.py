"""add documents source sha256 column

Revision ID: 2cf3722176c6
Revises: 7de8d00eccf5
Create Date: 2026-09-30 12:27:46.264310

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2cf3722176c6"
down_revision: str | None = "7de8d00eccf5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """A platform-independent content identity for the uploaded source file.

    Nullable: existing rows have no value to backfill from (the original
    upload bytes are not retained separately from the source file already on
    disk, and computing this retroactively from stored files is a distinct,
    separate concern from adding the column). Every row written by the
    current ingestion path populates it going forward.
    """
    op.add_column(
        "documents", sa.Column("source_sha256", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("documents", "source_sha256")
