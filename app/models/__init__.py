"""ORM models.

Every model must be imported here: Alembic's autogenerate only sees tables
that are attached to `Base.metadata` at the time `env.py` runs.
"""

from app.models.document import Document, DocumentStatus

__all__ = ["Document", "DocumentStatus"]
