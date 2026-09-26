"""Rate limiting for the public demo's extraction-triggering endpoint.

Scoped narrowly, the same way `app/api/demo_guard.py` is: one dependency,
added only to `POST /api/v1/documents/{id}/extract` -- the one route that
does real, non-trivial work per call (background extraction against the
demo provider) and the one a scripted loop against a public,
credential-free demo would actually target. Every other route (`/health`,
the unversioned health check, document reads, export, the review queue)
is untouched.

**In-process, not Redis, not a database table.** The demo's own
deployment (`docker-compose.yml`'s `demo` service) is one container --
there is no second process or replica this state would need to be shared
with, so an in-memory store is sufficient and avoids adding an external
service to satisfy a portfolio demo's abuse protection. It is also why
this limiter is deliberately unsuitable, as written, for a multi-replica
deployment: each replica would keep its own independent counts, a
documented limitation, not an oversight.

**Off unless explicitly enabled.** `Settings.demo_rate_limit_enabled`
defaults to `False`, so Live Mode and any deployment that never sets
`DOCINTEL_DEMO_RATE_LIMIT_ENABLED` is completely unaffected -- the
dependency below returns immediately without touching the request, the
clock, or the limiter's internal state.

**Two injectable seams, using this project's own existing
dependency-override pattern** (the same one `demo.app.create_demo_app()`
already uses for `get_provider`/`require_mutation_allowed`), so a test can
control both without a real sleep and without needing `TestClient` to fake
a distinct peer IP per call:

- `client_identity` -- a FastAPI dependency, overridable via
  `app.dependency_overrides[client_identity] = lambda: "some-id"`.
- `get_rate_limiter` -- a FastAPI dependency resolving the one
  `InMemoryRateLimiter` this process uses; a test overrides it with a
  fresh instance built on its own injected clock function.

**Not `X-Forwarded-For`.** No hosting target is chosen yet, and trusting a
client-supplied header with no configured, known proxy in front of this
process would let a visitor set their own header and evade the limit
entirely. `client_identity` reads the direct ASGI peer address
(`Request.client.host`) until a specific, trusted proxy mechanism is
deliberately added later.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings

#: The limiter's one window size. Not a setting -- the specification for
#: this feature names only a per-minute count
#: (`Settings.demo_rate_limit_per_minute`); the window it is measured over
#: is an implementation detail of *this* limiter, not something a
#: deployment should need to retune independently of that count.
WINDOW_SECONDS = 60.0

ClockFn = Callable[[], float]

#: No credential, no session, no cookie -- the only thing this limiter
#: ever keys on. `request.client` is `None` for some non-network ASGI
#: transports; falling those into one shared bucket ("unknown") is
#: deliberately conservative, never a way to bypass the limit.
_UNKNOWN_CLIENT = "unknown"


def client_identity(request: Request) -> str:
    """The direct ASGI peer address this request arrived from.

    A FastAPI dependency, not a plain function call, specifically so a
    test (or, later, a deployment with a known, trusted proxy in front of
    it) can replace it via `app.dependency_overrides` without touching
    this module -- the same seam `demo/app.py`'s two overrides already
    use.
    """
    client = request.client
    if client is None:
        return _UNKNOWN_CLIENT
    return client.host


class InMemoryRateLimiter:
    """A small, in-process, sliding-window request limiter.

    Keyed by whatever string `client_identity` (or a test's override)
    returns. State is a `deque` of hit timestamps per key, trimmed to the
    current window on every check -- no background sweep, no TTL thread,
    and nothing written to disk or to the database: state disappears the
    moment the process does.

    Thread-safe: FastAPI/Starlette can run a sync dependency in a worker
    thread, so concurrent requests can reach `check()` at once.
    """

    def __init__(self, clock: ClockFn = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(
        self, key: str, *, limit: int, window_seconds: float = WINDOW_SECONDS
    ) -> tuple[bool, float]:
        """Record one attempt for `key`.

        Returns `(allowed, retry_after_seconds)` -- `retry_after_seconds`
        is always `0.0` when `allowed` is `True`. Exactly `limit` calls
        within `window_seconds` of each other are allowed; the next one is
        not, until the oldest of that window has aged out.
        """
        now = self._clock()
        with self._lock:
            hits = self._hits[key]
            cutoff = now - window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= limit:
                retry_after = max(0.0, hits[0] + window_seconds - now)
                return False, retry_after

            hits.append(now)
            return True, 0.0

    def reset(self) -> None:
        """Drop all recorded state. For tests only."""
        with self._lock:
            self._hits.clear()


# One instance for this process -- cheap to construct, so no caching
# decorator is needed, just one shared object every request sees.
_limiter = InMemoryRateLimiter()


def get_rate_limiter() -> InMemoryRateLimiter:
    """The one rate limiter this process uses.

    A FastAPI dependency so a test can override it with a fresh
    `InMemoryRateLimiter` built on its own injected clock -- never a real
    sleep -- via `app.dependency_overrides[get_rate_limiter] = lambda: ...`.
    """
    return _limiter


def reset_rate_limiter() -> None:
    """Drop the shared limiter's recorded state. For tests only."""
    _limiter.reset()


def rate_limit_demo_extraction(
    settings: Annotated[Settings, Depends(get_settings)],
    identity: Annotated[str, Depends(client_identity)],
    limiter: Annotated[InMemoryRateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Refuse a request over `Settings.demo_rate_limit_per_minute` from the
    same client, with `429` and a `Retry-After` header, when
    `Settings.demo_rate_limit_enabled` is true.

    A no-op, touching neither the clock nor any stored state, when the
    setting is off -- the default, so Live Mode and any deployment that
    never opts in is unaffected. Never alters a successful response: this
    dependency either raises before the route body runs, or returns
    `None` and the request proceeds exactly as it would without it.
    """
    if not settings.demo_rate_limit_enabled:
        return

    allowed, retry_after = limiter.check(
        identity, limit=settings.demo_rate_limit_per_minute
    )
    if not allowed:
        retry_after_seconds = max(1, int(retry_after) + 1)
        raise HTTPException(
            status_code=429,
            detail=(
                "This demo allows "
                f"{settings.demo_rate_limit_per_minute} extraction "
                "requests per minute per visitor. Please wait and try "
                "again."
            ),
            headers={"Retry-After": str(retry_after_seconds)},
        )


__all__ = [
    "InMemoryRateLimiter",
    "client_identity",
    "get_rate_limiter",
    "rate_limit_demo_extraction",
    "reset_rate_limiter",
]
