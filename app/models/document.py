"""The `documents` table: one row per uploaded file."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.extraction import Extraction


class DocumentStatus(enum.StrEnum):
    """Lifecycle of a document as it moves through the pipeline."""

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"
    FAILED = "failed"


# Named explicitly so the Postgres type name is stable and the migration can
# create and drop it by that name.
document_status_enum = Enum(
    DocumentStatus,
    name="document_status",
    values_callable=lambda status: [member.value for member in status],
)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    filename: Mapped[str] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(String(1024))
    # Lowercase hex SHA-256 of the original uploaded bytes, computed before any
    # rendering. A stable, platform-independent content identity -- unlike a
    # rendered page image, the uploaded bytes are identical on every OS and
    # every rendering-library version, so this is what Demo Mode matches a
    # known sample document by (see demo/providers.py). Nullable because rows
    # written before this column existed have none; every row written by the
    # current ingestion path always gets one.
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(64))
    # Unknown until the pages are rendered (milestone 3).
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        document_status_enum,
        default=DocumentStatus.UPLOADED,
        server_default=DocumentStatus.UPLOADED.value,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    # Why a document ended up in FAILED. Set whenever status becomes FAILED and
    # cleared on a successful retry, so a failure is never silent.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    extractions: Mapped[list["Extraction"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="Extraction.created_at",
    )

    def __repr__(self) -> str:
        return f"<Document id={self.id} filename={self.filename!r} status={self.status}>"
