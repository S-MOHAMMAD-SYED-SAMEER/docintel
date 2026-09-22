"""The Demo Mode entrypoint: the real DocIntel application, with the
extraction provider dependency overridden to a deterministic, credential-free
stand-in.

Not a second application. `create_demo_app()` returns
`app.main.create_app()` itself -- the exact same FastAPI app, the exact same
upload, extract, review and export routes -- and overrides exactly one
FastAPI dependency, `app.providers.get_provider`, via
`app.dependency_overrides`, the same seam a test already uses to inject a
double. Nothing here duplicates a route, a validator, the confidence
scorer, persistence, the review queue, corrections, the audit trail, or
export.

Run with `uvicorn demo.app:app` -- the database still has to be a real,
reachable PostgreSQL instance, migrated to head (see docs/DEMO.md). No
Anthropic credential is read for extraction under this entrypoint, no
model is downloaded, and `DemoExtractionProvider` never reaches the
network.
"""

from fastapi import FastAPI

from app.main import create_app
from app.providers import get_provider
from demo.providers import DemoExtractionProvider


def create_demo_app() -> FastAPI:
    """The real application, with the extraction provider substituted at the
    same FastAPI dependency seam a test already uses.

    One instance of the demo provider, built once and named here explicitly
    -- deterministic construction, not a fresh instance re-loading its
    fixtures on every request the way returning the bare class itself as the
    override would.
    """
    app = create_app()

    demo_provider = DemoExtractionProvider()
    app.dependency_overrides[get_provider] = lambda: demo_provider

    return app


app = create_demo_app()


__all__ = ["app", "create_demo_app"]
