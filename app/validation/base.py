"""The result of running deterministic checks over an extraction.

Deliberately small: a check has a name, a verdict, the fields it bears on, and
which confidence signal it feeds. That is everything the scorer needs.
"""

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field


class CheckKind(enum.StrEnum):
    """Which confidence signal a check contributes to."""

    # Type, format and enum correctness — README signal 2.
    SCHEMA = "schema"
    # Totals and line-item arithmetic — README signal 3.
    ARITHMETIC = "arithmetic"


class CheckStatus(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    # The check could not run because a value it needs is absent. Not a
    # failure: an invoice that does not state a subtotal has not got the
    # arithmetic wrong, so the signal simply does not apply to those fields.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Check:
    name: str
    kind: CheckKind
    status: CheckStatus
    # Field names this check says something about. A cross-field check such as
    # `subtotal + tax = total` bears on all three.
    fields: tuple[str, ...]
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "status": str(self.status),
            "fields": list(self.fields),
            "detail": self.detail,
        }


def passed(name: str, kind: CheckKind, fields: Iterable[str], detail: str = "") -> Check:
    return Check(name, kind, CheckStatus.PASSED, tuple(fields), detail)


def failed(name: str, kind: CheckKind, fields: Iterable[str], detail: str) -> Check:
    return Check(name, kind, CheckStatus.FAILED, tuple(fields), detail)


def skipped(name: str, kind: CheckKind, fields: Iterable[str], detail: str) -> Check:
    return Check(name, kind, CheckStatus.SKIPPED, tuple(fields), detail)


@dataclass(frozen=True)
class ValidationReport:
    """Every check that ran, with lookups the scorer needs."""

    checks: tuple[Check, ...] = field(default_factory=tuple)

    def for_field(self, field_name: str) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if field_name in check.fields)

    def status_for(self, field_name: str, kind: CheckKind) -> CheckStatus:
        """One verdict for a field and signal.

        Any failure wins — a field that fails one arithmetic check is not
        rescued by passing another. With nothing but skips (or no checks at
        all) the signal does not apply to this field.
        """
        relevant = [
            check
            for check in self.for_field(field_name)
            if check.kind is kind and check.status is not CheckStatus.SKIPPED
        ]
        if not relevant:
            return CheckStatus.SKIPPED
        if any(check.status is CheckStatus.FAILED for check in relevant):
            return CheckStatus.FAILED
        return CheckStatus.PASSED

    def failures(self) -> tuple[Check, ...]:
        return tuple(
            check for check in self.checks if check.status is CheckStatus.FAILED
        )

    def as_list(self) -> list[dict[str, object]]:
        return [check.as_dict() for check in self.checks]
