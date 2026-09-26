"""Rate limiting for the demo's extraction-triggering endpoint:
`app/api/rate_limit.py`.

Uses the same `migrated_engine`/`storage_dir`/`demo.app.create_demo_app()`
seam `tests/test_demo_guard.py` already establishes for this deployment --
no real network, no real Anthropic call, and no real sleep: the limiter's
own clock and client-identity dependency are both injected, the same way
`demo.app.create_demo_app()` already injects the provider and the
mutation guard.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.rate_limit import (
    InMemoryRateLimiter,
    client_identity,
    get_rate_limiter,
    reset_rate_limiter,
)
from app.config import get_settings
from app.main import create_app
from demo.app import create_demo_app
from demo.seed import seed_demo_documents

# Comfortably above the default 10/minute, without running so many requests
# that this file becomes slow -- the point is "the limiter never engages",
# not "exhaustively prove no limiter ever would".
ABOVE_DEFAULT_LIMIT = 12


@pytest.fixture(autouse=True)
def _clean_shared_state() -> None:
    """Two kinds of process-wide state outlive any single test: the cached
    `Settings` (`app.config.get_settings`, an `lru_cache`) and the
    module-level rate limiter singleton `app.api.rate_limit` keeps. Both
    are cleared before and after every test here, so no test's environment
    variables or recorded request history leak into another's."""
    get_settings.cache_clear()
    reset_rate_limiter()
    yield
    get_settings.cache_clear()
    reset_rate_limiter()


class FakeClock:
    """A clock a test advances explicitly, so no test here ever sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _seed(migrated_engine: Engine) -> list:
    with Session(migrated_engine) as session:
        return [document.id for document in seed_demo_documents(session)]


def _enable_rate_limit(monkeypatch: pytest.MonkeyPatch, *, per_minute: int = 10) -> None:
    monkeypatch.setenv("DOCINTEL_DEMO_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("DOCINTEL_DEMO_RATE_LIMIT_PER_MINUTE", str(per_minute))
    get_settings.cache_clear()


# --- 1: a request below the limit succeeds normally -------------------------


def test_a_single_request_below_the_limit_succeeds(
    migrated_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_rate_limit(monkeypatch, per_minute=10)
    document_ids = _seed(migrated_engine)
    client = TestClient(create_demo_app())

    response = client.post(f"/api/v1/documents/{document_ids[0]}/extract")

    assert response.status_code == 202


# --- 2 & 3: exactly N succeed within the window, N+1 is refused -------------


def test_exactly_the_configured_limit_succeeds_then_the_next_is_refused(
    migrated_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = 3
    _enable_rate_limit(monkeypatch, per_minute=limit)
    document_ids = _seed(migrated_engine)

    app = create_demo_app()
    limiter = InMemoryRateLimiter(clock=FakeClock())
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    client = TestClient(app)

    for _ in range(limit):
        response = client.post(f"/api/v1/documents/{document_ids[0]}/extract")
        assert response.status_code == 202

    blocked = client.post(f"/api/v1/documents/{document_ids[0]}/extract")

    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert int(blocked.headers["Retry-After"]) > 0


# --- 4: different client identities have independent limits -----------------


def test_different_clients_have_independent_limits(
    migrated_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_rate_limit(monkeypatch, per_minute=1)
    document_ids = _seed(migrated_engine)

    shared_limiter = InMemoryRateLimiter(clock=FakeClock())

    app_a = create_demo_app()
    app_a.dependency_overrides[get_rate_limiter] = lambda: shared_limiter
    app_a.dependency_overrides[client_identity] = lambda: "visitor-a"
    client_a = TestClient(app_a)

    app_b = create_demo_app()
    app_b.dependency_overrides[get_rate_limiter] = lambda: shared_limiter
    app_b.dependency_overrides[client_identity] = lambda: "visitor-b"
    client_b = TestClient(app_b)

    first = client_a.post(f"/api/v1/documents/{document_ids[0]}/extract")
    assert first.status_code == 202
    second = client_a.post(f"/api/v1/documents/{document_ids[0]}/extract")
    assert second.status_code == 429

    third = client_b.post(f"/api/v1/documents/{document_ids[1]}/extract")
    assert third.status_code == 202


# --- 5: advancing the injected clock frees a blocked client -----------------


def test_advancing_the_clock_allows_a_blocked_client_to_request_again(
    migrated_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_rate_limit(monkeypatch, per_minute=1)
    document_ids = _seed(migrated_engine)

    clock = FakeClock()
    app = create_demo_app()
    limiter = InMemoryRateLimiter(clock=clock)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    client = TestClient(app)

    assert client.post(f"/api/v1/documents/{document_ids[0]}/extract").status_code == 202
    assert client.post(f"/api/v1/documents/{document_ids[0]}/extract").status_code == 429

    clock.advance(60.0)  # the full window -- never a real sleep

    assert client.post(f"/api/v1/documents/{document_ids[0]}/extract").status_code == 202


# --- 6: health/readiness are unaffected by an exhausted limit ---------------


def test_health_remains_available_after_exhausting_the_limit(
    migrated_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_rate_limit(monkeypatch, per_minute=1)
    document_ids = _seed(migrated_engine)

    app = create_demo_app()
    limiter = InMemoryRateLimiter(clock=FakeClock())
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    client = TestClient(app)

    assert client.post(f"/api/v1/documents/{document_ids[0]}/extract").status_code == 202
    assert client.post(f"/api/v1/documents/{document_ids[0]}/extract").status_code == 429

    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/health").status_code == 200


# --- 7 & 8: off by default, live/production behaviour is unaffected --------


def test_the_limiter_is_disabled_by_default(
    migrated_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `DOCINTEL_DEMO_RATE_LIMIT_ENABLED` set at all -- the default --
    so well more than the default 10/minute still succeeds."""
    monkeypatch.delenv("DOCINTEL_DEMO_RATE_LIMIT_ENABLED", raising=False)
    get_settings.cache_clear()
    document_ids = _seed(migrated_engine)
    client = TestClient(create_demo_app())

    for _ in range(ABOVE_DEFAULT_LIMIT):
        response = client.post(f"/api/v1/documents/{document_ids[0]}/extract")
        assert response.status_code == 202


def test_live_mode_extraction_is_unaffected_when_the_limiter_is_disabled(
    api_client: TestClient,
    migrated_engine: Engine,
    fake_provider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real production app (`api_client`, from `app.main.create_app()`),
    not the demo app -- proving this feature, off by default, changes
    nothing about Live Mode's own `/extract` dispatch."""
    import uuid

    from app.models import Document
    from app.providers import get_provider

    monkeypatch.delenv("DOCINTEL_DEMO_RATE_LIMIT_ENABLED", raising=False)
    get_settings.cache_clear()

    upload = api_client.post(
        "/api/v1/documents",
        files={
            "file": (
                "acme-invoice.pdf",
                (
                    Path(__file__).resolve().parent.parent
                    / "evals"
                    / "datasets"
                    / "invoices_v1"
                    / "invoice-002.pdf"
                ).read_bytes(),
                "application/pdf",
            )
        },
        data={"doc_type": "invoice"},
    )
    assert upload.status_code == 201
    document_id = uuid.UUID(upload.json()["id"])

    with Session(migrated_engine) as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.page_count is not None

    api_client.app.dependency_overrides[get_provider] = lambda: fake_provider

    for _ in range(ABOVE_DEFAULT_LIMIT):
        response = api_client.post(f"/api/v1/documents/{document_id}/extract")
        assert response.status_code == 202

    del api_client.app.dependency_overrides[get_provider]
