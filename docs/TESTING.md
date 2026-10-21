# Testing DocIntel

What the tests are, how to run them, what each environment skips and why, and what a green run does not show. Setup is in
[SETUP.md](SETUP.md); the evaluations (which measure extraction and are not tests) are in the README and [EVAL.md](EVAL.md).

## Run them

```bash
ruff check .          # lint; CI runs it first
pytest                # the whole suite, from the repository root
pytest tests/test_hardening.py -q          # one file
```

The suite needs **no API key and makes no network call**: an autouse fixture in `tests/conftest.py` patches the Anthropic
client's request methods to raise, and a test asserts that guard fires. Tests that need PostgreSQL **skip rather than fail**
when no server answers; for a real run start one and name the test database (its name must end in `_test`, because the
fixtures migrate and drop it):

```bash
export DOCINTEL_TEST_DATABASE_URL=postgresql+psycopg://docintel:docintel@localhost:5432/docintel_test
pytest
```

The schema is built by running the **real Alembic migrations**, not a shortcut. A full run takes about 1.5 to 5 minutes.

## What is covered

About 34 test files, in groups:

| Group | What it checks |
|---|---|
| Ingestion and rendering: `test_upload`, `test_media`, `test_storage`, `test_ingestion`, `test_rendering`, `test_text_layer` | File-signature validation, safe filenames, size caps, PDF and image rendering, the text layer |
| Extraction: `test_extraction`, `test_extract_endpoint`, `test_invoice_schema`, `test_purchase_order` | The pipeline against a fake provider, the registries and the proof that nothing above them names a document type |
| The provider and the failure paths: `test_providers`, `test_hardening` | The Anthropic adapter against a stubbed client; the schema sent to the API has no keyword the endpoint rejects while the limits are still enforced on the way back in; each failure becomes the right error class; every attempt writes its row; the page and byte guards; one extraction per document; `GET /documents/{id}`, the page and source endpoints; the review page's source images and failed-documents list; the evaluation budget and its stop rule |
| Validation and scoring: `test_validation`, `test_confidence`, `test_scoring_persistence` | The deterministic checks, the four-signal score, the review rule, persistence |
| Review and export: `test_review_api`, `test_review_page`, `test_corrections`, `test_export` | The queue, corrections and their audit trail, JSON and CSV export |
| Evaluation: `test_eval_dataset`, `test_eval_metrics`, `test_eval_run`, `test_eval_cli` | Dataset loading, the metrics arithmetic, the runner and the command line |
| Demo: `test_demo_*`, `test_fixture_bytes` | The demo app, its mutation guard and rate limiter, and that the committed sample PDFs are byte-exact on every platform |
| Config, database, migrations: `test_config`, `test_db`, `test_documents_model`, `test_migrations`, `test_health` | Settings, the schema, upgrade/downgrade/re-upgrade, health |

Two checks stop a claim drifting from the evidence: `test_fixture_bytes.py` (the demo recognises documents by the SHA-256 of
their bytes, so a checkout that changed a PDF breaks it, and it did once; see SETUP.md) and the migration tests.

## What each environment runs

CI has four jobs, none with a key: **`test`** (Linux, a PostgreSQL service, `ruff check .`, the whole suite), **`windows`**
(everything that needs no database), **`docker-build`** (builds the image and runs the Compose stack, then extracts a seeded
invoice through the demo and checks the record) and **`audit`** (`pip-audit`).

| Environment | Result (2026-10-21, after the real runs) | Why the rest is skipped |
|---|---|---|
| CI, Linux (`test`) | 641 collected, 641 passed | nothing |
| CI, Windows (`windows`) | 374 passed, 267 skipped | a Windows runner has no PostgreSQL service, so every database test skips; it exists to catch what only Windows shows |
| No database, anywhere | 374 passed, 267 skipped | the same tests |

## What a green run does not show

Passing tests show the code does what the tests say. They do not show that the extraction is accurate: no model is called in
the suite (every provider is a fake), so a change in the model's behaviour, a wrong model ID, or an API that rejects the
schema cannot turn a test red. That is what the evaluation runs are for, and the first of them (a single smoke extraction)
is also the first check that the live endpoint accepts the schema. See [EVAL.md](EVAL.md) for the plan.

## Adding a test

Put it beside its group, use `FakeProvider` from `tests/conftest.py` rather than a real model, and use the `migrated_engine`
and `api_client` fixtures for database and API work (they skip cleanly without PostgreSQL). Anything that quotes a number from
a results file should read it from the file.
