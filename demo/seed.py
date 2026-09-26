"""Seeding the demo's three known sample invoices.

The demo's mutation guard (`app/api/demo_guard.py`) refuses `POST
/api/v1/documents` outright, so a visitor can never upload anything --
which means the demo has nothing to show unless its three recognised
documents (`docs/DEMO.md` §5) already exist before the first request.

This reuses the real pipeline's own functions
(`app.ingestion.store_upload`, `app.ingestion.render_pages`) -- the same
two calls `POST /api/v1/documents` itself makes -- never a second upload
implementation. Extraction is deliberately *not* run here: triggering
`POST /api/v1/documents/{id}/extract` on an already-seeded document is
the interactive, rate-limited step a visitor takes themselves, which is
most of the point of a "watch the real pipeline work" demo.

Idempotent by a fixed, deterministic id per fixture (`uuid.uuid5`), not by
a special-cased flag: a document that already exists is left alone,
exactly the idempotency contract `demo.seed.seed_demo_documents` is
called under on every demo startup.
"""

import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import ingestion
from app.models import Document

logger = logging.getLogger(__name__)

# A fixed namespace, distinct from any other UUID this project mints, so a
# fixture's seeded document id can never collide with a real upload's
# randomly generated one.
_SEED_NAMESPACE = uuid.UUID("6a3b6f0a-8f1a-4b8e-9b8b-2b6a6a1f0c5e")

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "evals" / "datasets" / "invoices_v1"

# The same three fixture ids `demo/fixtures/invoice_answers.json` and
# `docs/DEMO.md` §5 name -- kept as a small, explicit list rather than
# read from the fixtures file, so a change to the recognised set is a
# deliberate edit in both places, not a silent, automatic one here.
SEEDED_FIXTURE_IDS = ("invoice-001", "invoice-003", "invoice-004")


def seed_document_id(fixture_id: str) -> uuid.UUID:
    """The fixed id this fixture's seeded document always gets."""
    return uuid.uuid5(_SEED_NAMESPACE, fixture_id)


def seed_demo_documents(session: Session) -> list[Document]:
    """Seed the three known sample invoices, idempotently.

    Each is uploaded and rendered through the real pipeline, exactly as a
    visitor's own upload would be if the guard allowed one. Returns every
    seeded document, newly created or already present.
    """
    documents: list[Document] = []
    for fixture_id in SEEDED_FIXTURE_IDS:
        document_id = seed_document_id(fixture_id)
        existing = session.get(Document, document_id)
        if existing is not None:
            documents.append(existing)
            continue

        pdf_path = FIXTURE_DIR / f"{fixture_id}.pdf"
        document = ingestion.store_upload(
            session,
            filename=f"{fixture_id}.pdf",
            doc_type="invoice",
            data=pdf_path.read_bytes(),
            document_id=document_id,
        )
        document = ingestion.render_pages(session, document)
        logger.info("seeded demo document %s (%s)", document.id, fixture_id)
        documents.append(document)

    return documents


def _main() -> None:
    """`python -m demo.seed` -- a one-line command for Docker Compose's own
    seed step, mirroring `docs/DEMO.md`'s manual instructions. Builds the
    session the same way the application itself does
    (`app.db.session.get_sessionmaker`), already driven by
    `DOCINTEL_DATABASE_URL` -- no new configuration surface."""
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as session:
        documents = seed_demo_documents(session)
        for fixture_id, document in zip(SEEDED_FIXTURE_IDS, documents, strict=True):
            print(f"{fixture_id}: {document.id}")
        print(f"seeded {len(documents)} demo document(s)")


if __name__ == "__main__":
    _main()


__all__ = ["SEEDED_FIXTURE_IDS", "seed_demo_documents", "seed_document_id"]
