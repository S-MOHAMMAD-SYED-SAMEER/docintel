"""The Demo Mode entrypoint: the real DocIntel application, with the
extraction provider and the mutation guard dependencies overridden to
demo-only implementations.

Not a second application. `create_demo_app()` returns
`app.main.create_app()` itself -- the exact same FastAPI app, the exact same
upload, extract, review and export routes -- and overrides two FastAPI
dependencies, `app.providers.get_provider` and
`app.api.demo_guard.require_mutation_allowed`, via
`app.dependency_overrides`, the same seam a test already uses to inject a
double. Nothing here duplicates a route, a validator, the confidence
scorer, persistence, the review queue, corrections, the audit trail, or
export.

`get_provider`'s override is resolved fresh per request (never a single
shared instance), from that request's own `document_id` path parameter --
the same FastAPI sub-dependency mechanism `extract_document`'s own
`session` parameter already relies on. It looks up the document's
`source_sha256` (the uploaded source PDF's own SHA-256, computed once at
ingestion, before any rendering) and binds a `DemoExtractionProvider` to
whichever fixture, if any, matches that identity -- never to the rendered
page images `extract()` will later receive, which is exactly the
platform-dependent value this design avoids depending on. See
`demo/providers.py`.

**No new uploads or corrections.** `require_mutation_allowed` is
overridden to `app.api.demo_guard.demo_mutation_refused`, so `POST
/api/v1/documents`, `POST /api/v1/review/{field_id}`, and `POST
/review/{field_id}` all refuse with 403 here. That would leave the demo
with nothing to show, since nothing can ever be uploaded -- which is why
`demo/seed.py` exists: it seeds the three sample invoices `docs/DEMO.md`
§5 documents, through the same real `app.ingestion` functions the upload
route itself calls, before the app is ever reachable (see
`docker-compose.yml`'s `seed` service). Triggering extraction and
exporting a record on those seeded documents are unaffected -- the
guard is never applied to either route.

Run with `uvicorn demo.app:app` -- the database still has to be a real,
reachable PostgreSQL instance, migrated to head and seeded (see
docs/DEMO.md). No Anthropic credential is read for extraction under this
entrypoint, no model is downloaded, and `DemoExtractionProvider` never
reaches the network.
"""

import uuid
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.api.demo_guard import demo_mutation_refused, require_mutation_allowed
from app.db.session import get_session
from app.main import create_app
from app.models import Document
from app.providers import ExtractionProvider, get_provider
from demo.providers import DemoExtractionProvider


def _demo_provider_for_request(
    document_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ExtractionProvider:
    """The demo's `get_provider` override -- resolved fresh per request.

    `document_id` is bound from the route's own path parameter, the same
    way `session` already is; FastAPI resolves both from the same request
    that `extract_document` itself handles, so this reads the identical
    row and the identical session, not a second connection.

    A document with no row, or no `source_sha256`, resolves to "no fixture
    matched" here -- not an error raised in this dependency. The actual
    `ProviderError` for an unrecognised document is raised later, from
    `DemoExtractionProvider.extract()`, at the exact point (and through the
    exact `_record_provider_failure` handling) a real provider failure
    already goes through -- this dependency only ever decides *which*
    fixture, if any, that later call will see.
    """
    document = session.get(Document, document_id)
    source_sha256 = document.source_sha256 if document is not None else None
    return DemoExtractionProvider.for_source_sha256(source_sha256)


def create_demo_app() -> FastAPI:
    """The real application, with the extraction provider and the mutation
    guard substituted at the same FastAPI dependency seam a test already
    uses.
    """
    app = create_app()

    app.dependency_overrides[get_provider] = _demo_provider_for_request
    app.dependency_overrides[require_mutation_allowed] = demo_mutation_refused

    return app


app = create_demo_app()


__all__ = ["app", "create_demo_app"]
