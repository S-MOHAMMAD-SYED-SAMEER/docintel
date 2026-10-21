"""The invoice prompt versions: v1 is untouched, v2 is v1 plus one paragraph, and the setting chooses between them."""

import hashlib

import pytest

from app.config import get_settings
from app.extractors import UnknownDocumentType, get_extractor
from app.extractors.invoice import IDENTIFIER_NOTE, PROMPT, PROMPT_V2

#: sha256 of the invoice/v1 prompt as it was when the first real runs were made. v1 is never edited: a change is a new version.
V1_SHA256 = "d82f6d675d76194ba219d9290980b006205e3932bd6e4fe70daae9909addeffa"


@pytest.fixture
def prompt_version(monkeypatch: pytest.MonkeyPatch):
    def choose(version: str | None) -> None:
        if version is None:
            monkeypatch.delenv("DOCINTEL_INVOICE_PROMPT_VERSION", raising=False)
        else:
            monkeypatch.setenv("DOCINTEL_INVOICE_PROMPT_VERSION", version)
        get_settings.cache_clear()

    yield choose
    get_settings.cache_clear()


def test_v1_is_byte_for_byte_what_the_first_real_runs_used() -> None:
    assert hashlib.sha256(PROMPT.encode()).hexdigest() == V1_SHA256


def test_v2_is_v1_plus_exactly_one_paragraph_about_identifiers() -> None:
    v1_lines, v2_lines = PROMPT.splitlines(), PROMPT_V2.splitlines()
    added = [line for line in v2_lines if line not in v1_lines]

    assert len(v2_lines) == len(v1_lines) + 1
    assert [line for line in v1_lines if line not in v2_lines] == []  # nothing removed or reworded
    assert len(added) == 1 and added[0].startswith("- Identifiers (")
    assert " ".join(added[0].split()) == " ".join(IDENTIFIER_NOTE.split())
    assert "zero-width space" in PROMPT_V2 and "vendor_tax_id" in PROMPT_V2


def test_the_default_is_v2_as_the_decision_rule_in_eval_md_decided(prompt_version) -> None:
    prompt_version(None)

    extractor = get_extractor("invoice")

    assert extractor.prompt_version == "invoice/v2" and extractor.prompt == PROMPT_V2


def test_v1_is_still_selectable(prompt_version) -> None:
    prompt_version("invoice/v1")

    extractor = get_extractor("invoice")

    assert extractor.prompt_version == "invoice/v1" and extractor.prompt == PROMPT


def test_the_setting_selects_v2_and_the_schema_is_the_same(prompt_version) -> None:
    prompt_version("invoice/v2")

    extractor = get_extractor("invoice")

    assert extractor.prompt_version == "invoice/v2" and extractor.prompt == PROMPT_V2
    prompt_version("invoice/v1")
    assert get_extractor("invoice").schema is extractor.schema


def test_an_unknown_version_fails_loudly(prompt_version) -> None:
    prompt_version("invoice/v9")

    with pytest.raises(UnknownDocumentType, match="invoice/v9"):
        get_extractor("invoice")


def test_the_purchase_order_prompt_is_not_affected(prompt_version) -> None:
    prompt_version("invoice/v2")

    assert get_extractor("purchase_order").prompt_version == "purchase_order/v1"
