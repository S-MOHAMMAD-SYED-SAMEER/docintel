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

from fastapi import FastAPI

from app.api.demo_guard import demo_mutation_refused, require_mutation_allowed
from app.main import create_app
from app.providers import get_provider
from demo.providers import DemoExtractionProvider


def create_demo_app() -> FastAPI:
    """The real application, with the extraction provider and the mutation
    guard substituted at the same FastAPI dependency seam a test already
    uses.

    One instance of the demo provider, built once and named here explicitly
    -- deterministic construction, not a fresh instance re-loading its
    fixtures on every request the way returning the bare class itself as the
    override would.
    """
    app = create_app()

    demo_provider = DemoExtractionProvider()
    app.dependency_overrides[get_provider] = lambda: demo_provider
    app.dependency_overrides[require_mutation_allowed] = demo_mutation_refused

    return app


app = create_demo_app()


__all__ = ["app", "create_demo_app"]
