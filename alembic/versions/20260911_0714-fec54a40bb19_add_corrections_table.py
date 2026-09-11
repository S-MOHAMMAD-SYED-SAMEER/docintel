"""add corrections table

Revision ID: fec54a40bb19
Revises: 1a624e31fbf0
Create Date: 2026-09-11 07:14:02.881517

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fec54a40bb19"
down_revision: str | None = "1a624e31fbf0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """One row per human correction, recording what the field used to say."""
    op.create_table(
        "corrections",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("field_value_id", sa.Uuid(), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("corrected_value", sa.Text(), nullable=True),
        sa.Column(
            "corrected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["field_value_id"],
            ["field_values.id"],
            name=op.f("fk_corrections_field_value_id_field_values"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corrections")),
    )
    op.create_index(
        op.f("ix_corrections_field_value_id"),
        "corrections",
        ["field_value_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_corrections_field_value_id"), table_name="corrections")
    op.drop_table("corrections")
