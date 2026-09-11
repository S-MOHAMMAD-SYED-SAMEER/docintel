"""add documents error column

Revision ID: 6f9807e549bb
Revises: 86bfd9158ecb
Create Date: 2026-09-11 06:24:33.555868

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6f9807e549bb"
down_revision: str | None = "86bfd9158ecb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record why a document failed, alongside the FAILED status."""
    op.add_column("documents", sa.Column("error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "error")
