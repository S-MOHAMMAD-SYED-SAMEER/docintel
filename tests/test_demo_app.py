"""demo.app: the real application, with only the provider swapped.

Proves the demo entrypoint reuses the production routes exactly, that the
extraction provider dependency is overridden for it, and -- critically --
that this override is scoped to the demo app instance only: the normal
`app.main.create_app()` FastAPI app is completely unaffected.
"""

from fastapi.testclient import TestClient

from app.main import create_app
from app.providers import get_provider
from demo.app import create_demo_app
from demo.providers import DemoExtractionProvider


def test_the_demo_app_exposes_exactly_the_production_routes() -> None:
    production_paths = sorted(create_app().openapi()["paths"])
    demo_paths = sorted(create_demo_app().openapi()["paths"])

    assert demo_paths == production_paths
    assert "/demo/extract" not in demo_paths


def test_the_demo_app_overrides_only_the_provider_dependency() -> None:
    demo_app = create_demo_app()

    assert get_provider in demo_app.dependency_overrides
    resolved = demo_app.dependency_overrides[get_provider]()
    assert isinstance(resolved, DemoExtractionProvider)


def test_the_production_app_is_unaffected_by_the_demo_override() -> None:
    """The two are separate FastAPI instances: overriding demo_app's
    provider must never leak into a freshly built production app."""
    create_demo_app()  # built and discarded, as any request-time import would
    production_app = create_app()

    assert get_provider not in production_app.dependency_overrides


def _flatten_routes(routes):
    """FastAPI nests an included router's routes behind its own wrapper
    object rather than exposing them directly on `app.routes`."""
    flat = []
    for route in routes:
        if hasattr(route, "path"):
            flat.append(route)
        elif hasattr(route, "original_router"):
            flat.extend(_flatten_routes(route.original_router.routes))
    return flat


def test_the_demo_app_and_the_production_app_share_the_same_route_objects() -> None:
    """Not a rebuilt/duplicated router: `create_demo_app()` calls
    `app.main.create_app()` itself, so the underlying route functions --
    upload, extract, review, export -- are the exact same code objects."""
    # The leaf route's own `.path` is relative to its router; the `/api/v1`
    # prefix is applied by an ancestor router and isn't visible here.
    extract_path = "/documents/{document_id}/extract"
    production_extract = next(
        route for route in _flatten_routes(create_app().routes) if route.path == extract_path
    )
    demo_extract = next(
        route
        for route in _flatten_routes(create_demo_app().routes)
        if route.path == extract_path
    )

    assert production_extract.endpoint is demo_extract.endpoint
