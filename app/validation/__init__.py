"""Deterministic validation.

One module per document type, registered here. The pipeline asks for a
validator by `doc_type` and never names a type itself — the same shape as
`app/extractors/`.
"""

from collections.abc import Callable
from typing import Any

from app.validation.base import (
    Check,
    CheckKind,
    CheckStatus,
    ValidationReport,
)
from app.validation.invoice import validate_invoice

Validator = Callable[[Any], ValidationReport]

_VALIDATORS: dict[str, Validator] = {
    "invoice": validate_invoice,
}


def no_checks(parsed: Any) -> ValidationReport:
    """A document type with no deterministic rules yet runs no checks.

    An empty report is not a pass and not a failure: every deterministic signal
    simply does not apply, and scoring falls back to the signals that do.
    """
    return ValidationReport()


def get_validator(doc_type: str) -> Validator:
    return _VALIDATORS.get(doc_type, no_checks)


__all__ = [
    "Check",
    "CheckKind",
    "CheckStatus",
    "ValidationReport",
    "Validator",
    "get_validator",
    "no_checks",
    "validate_invoice",
]
