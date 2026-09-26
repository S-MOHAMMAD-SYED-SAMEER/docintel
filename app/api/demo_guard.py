"""The demo mutation guard.

A no-op by default. Production/Live Mode never blocks a mutation through
this dependency -- it exists purely as a FastAPI dependency seam for
`demo/app.py` to override, the exact same seam `app.providers.get_provider`
is already overridden through there.

No settings flag, no module-level "is this the demo" boolean: whether a
mutation is refused is decided entirely by *which FastAPI application
instance* resolves this dependency -- `app.main.create_app()`'s own
instance never touches `app.dependency_overrides`, so it always gets this
function's real body (allow); `demo.app.create_demo_app()`'s instance
overrides it to always refuse. There is no mutable demo-mode state
anywhere for either behaviour to depend on.
"""

from fastapi import HTTPException, status


def require_mutation_allowed() -> None:
    """Allow the mutation. The production default -- does nothing.

    Kept intentionally trivial: this function's only job is to exist as a
    stable object `app.dependency_overrides` can key on. All of the actual
    "refuse in the demo" behaviour lives in `demo/app.py::_deny_mutations`,
    the override -- never here.
    """
    return None


def demo_mutation_refused() -> None:
    """The demo's override body: unconditionally refuse.

    A free function, not a closure, so a test can call it directly without
    building a demo app first, and so `demo/app.py` has nothing to
    construct beyond referencing it.
    """
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "This is a public, credential-free demo of DocIntel. It "
            "serves three pre-seeded sample invoices read-only through "
            "the real pipeline; uploading a new document and submitting "
            "a correction are both disabled here. Triggering extraction "
            "and exporting a record on the seeded documents still work "
            "-- see docs/DEMO.md."
        ),
    )


__all__ = ["demo_mutation_refused", "require_mutation_allowed"]
