"""The `corrections` table: what a human changed, and what it used to say.

`field_values.value` always holds the *current* value, so a correction row is
the audit record of a supersession: `original_value` is what the field said
immediately before this correction, `corrected_value` is what it says now.
Correcting a field twice leaves two rows whose values chain back to the model's
original answer, so nothing is ever silently discarded.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.extraction import FieldValue


class Correction(Base):
    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    field_value_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("field_values.id", ondelete="CASCADE"),
        index=True,
    )
    # What the field held before this correction. Null when the model found
    # nothing and the reviewer supplied the first value.
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the reviewer says the document actually says. Null is allowed: a
    # reviewer clearing a hallucinated value is a correction too.
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    field_value: Mapped["FieldValue"] = relationship(back_populates="corrections")

    def __repr__(self) -> str:
        return (
            f"<Correction {self.original_value!r} -> {self.corrected_value!r} "
            f"at {self.corrected_at}>"
        )
