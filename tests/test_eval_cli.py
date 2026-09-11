"""The `python -m evals.run` command line."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import EvalRun
from evals.run import EXIT_BAD_USAGE, EXIT_FAILED_DOCUMENTS, EXIT_OK, main

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cli_env(migrated_engine: Engine, storage_dir: Path, monkeypatch):
    """Settings and caches pointed at the test database and temp storage."""
    from app.config import get_settings
    from app.db.session import reset_engine

    get_settings.cache_clear()
    reset_engine()
    yield
    get_settings.cache_clear()
    reset_engine()


def _run(args: list[str], capsys) -> tuple[int, str]:
    code = main(args)
    return code, capsys.readouterr().out


# --- happy path -----------------------------------------------------------


def test_a_valid_dataset_runs_and_exits_zero(cli_env, capsys) -> None:
    code, out = _run(
        ["--dataset", "invoices_v1", "--provider", "stub", "--limit", "2"], capsys
    )

    assert code == EXIT_OK
    assert "Evaluation: invoices_v1" in out


def test_the_output_contains_every_required_metric(cli_env, capsys) -> None:
    _, out = _run(
        ["--dataset", "invoices_v1", "--provider", "stub", "--limit", "2"], capsys
    )

    for required in (
        "Evaluation: invoices_v1",
        "model ",
        "prompt version",
        "Exact-match accuracy per field",
        "invoice_number",
        "review rate",
        "FALSE-CONFIDENT RATE",
        "Cost",
        "Latency",
        "p50",
        "p95",
    ):
        assert required in out, required


def test_the_output_warns_that_the_dataset_is_synthetic(cli_env, capsys) -> None:
    _, out = _run(
        ["--dataset", "invoices_v1", "--provider", "stub", "--limit", "1"], capsys
    )

    assert "synthetic" in out
    assert "not real-world accuracy" in out


def test_the_output_warns_when_the_stub_provider_was_used(cli_env, capsys) -> None:
    _, out = _run(
        ["--dataset", "invoices_v1", "--provider", "stub", "--limit", "1"], capsys
    )

    assert "measure the harness, not extraction quality" in out


def test_the_run_is_persisted(cli_env, migrated_engine: Engine, capsys) -> None:
    _run(["--dataset", "invoices_v1", "--provider", "stub", "--limit", "2"], capsys)

    with Session(migrated_engine) as session:
        run = session.execute(select(EvalRun)).scalar_one()

    assert run.dataset_name == "invoices_v1"
    assert run.model_name == "stub-provider"
    assert run.prompt_version == "invoice/v1"
    assert run.document_count == 2


def test_no_persist_writes_nothing(cli_env, migrated_engine: Engine, capsys) -> None:
    _run(
        [
            "--dataset",
            "invoices_v1",
            "--provider",
            "stub",
            "--limit",
            "1",
            "--no-persist",
        ],
        capsys,
    )

    with Session(migrated_engine) as session:
        assert session.execute(select(EvalRun)).scalars().all() == []


def test_json_output_is_machine_readable(cli_env, capsys) -> None:
    _, out = _run(
        [
            "--dataset",
            "invoices_v1",
            "--provider",
            "stub",
            "--limit",
            "1",
            "--json",
            "--no-persist",
        ],
        capsys,
    )

    payload = json.loads(out[out.index("{") :])
    assert payload["dataset"] == "invoices_v1"
    assert "false_confident_rate" in payload
    assert "latency_ms" in payload


def test_limit_restricts_the_documents_evaluated(cli_env, capsys) -> None:
    _, out = _run(
        [
            "--dataset",
            "invoices_v1",
            "--provider",
            "stub",
            "--limit",
            "3",
            "--no-persist",
        ],
        capsys,
    )

    assert "documents        3" in out


# --- failure paths --------------------------------------------------------


def test_an_unknown_dataset_fails_clearly(cli_env, capsys) -> None:
    code = main(["--dataset", "does-not-exist", "--provider", "stub"])
    captured = capsys.readouterr()

    assert code == EXIT_BAD_USAGE
    assert "No dataset named 'does-not-exist'" in captured.err
    assert "invoices_v1" in captured.err


def test_a_bad_limit_fails_clearly(cli_env, capsys) -> None:
    code = main(["--dataset", "invoices_v1", "--provider", "stub", "--limit", "0"])

    assert code == EXIT_BAD_USAGE
    assert "--limit must be at least 1" in capsys.readouterr().err


def test_failed_documents_exit_non_zero(cli_env, capsys, monkeypatch) -> None:
    """A broken run must not look like a good one."""
    from app.providers import ProviderError
    from evals import providers as providers_module

    class BrokenProvider:
        model_name = "broken"

        def extract(self, images, schema, prompt):
            raise ProviderError("boom")

    monkeypatch.setattr(providers_module, "build", lambda choice: BrokenProvider())

    code = main(
        ["--dataset", "invoices_v1", "--provider", "stub", "--limit", "2",
         "--no-persist"]
    )
    captured = capsys.readouterr()

    assert code == EXIT_FAILED_DOCUMENTS
    assert "FAILURES (2)" in captured.out
    assert "2 document(s) failed to extract" in captured.err


def test_an_unknown_provider_is_rejected_by_argparse(cli_env) -> None:
    with pytest.raises(SystemExit):
        main(["--dataset", "invoices_v1", "--provider", "magic"])


def test_the_module_is_executable(tmp_path: Path) -> None:
    """`python -m evals.run` is the documented entry point."""
    result = subprocess.run(
        [sys.executable, "-m", "evals.run", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0
    assert "--dataset" in result.stdout
    assert "--provider" in result.stdout
