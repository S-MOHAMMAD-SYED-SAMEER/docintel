# Setting DocIntel up

The README has the overview and the API; this file is the procedure, from a clean machine to a running app, the tests and
the evaluations. Commands are for a POSIX shell; Windows differences are marked.

| You want | You need |
|---|---|
| The standalone demo (three sample invoices, no model) | Docker. Nothing else: no key, no Python, no database install. |
| The tests | Python 3.13 and, for the tests that use one, PostgreSQL 16 (a plain `postgres` image is enough; there is no extension). No key. |
| The real app and a real evaluation | Python 3.13, PostgreSQL 16 and an Anthropic API key with credits (the API has no free tier). |

## 1. The quickest look: the demo stack

```bash
docker compose up --build
```

This starts PostgreSQL, applies the migrations, seeds three sample invoices and starts both the app (`:8000`) and the demo
(`:8001`), each step waiting for the one before it. Open `http://localhost:8001/review` after triggering an extraction:

```bash
curl -X POST localhost:8001/api/v1/documents/1d1d8194-747f-58a2-b9c2-9397bca02ea8/extract
curl localhost:8001/api/v1/documents/1d1d8194-747f-58a2-b9c2-9397bca02ea8        # status
```

The demo answers with committed values (the labels of the sample invoices, with hand-authored confidences), not a model;
[DEMO.md](DEMO.md) says exactly what it does and does not show. CI runs this stack on every push.

## 2. Python environment

```bash
python3.13 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e ".[dev]"             # includes pytest and ruff
```

## 3. PostgreSQL

```bash
docker run -d --name docintel-db -p 5432:5432 \
  -e POSTGRES_USER=docintel -e POSTGRES_PASSWORD=docintel -e POSTGRES_DB=docintel postgres:16
docker exec docintel-db createdb -U docintel docintel_test     # the suite migrates and drops this one
```

`DOCINTEL_DATABASE_URL` is the application's database and `DOCINTEL_TEST_DATABASE_URL` the test suite's; the test fixtures
refuse any database whose name does not end in `_test`. If either is already set in your environment (a user-level Windows
variable persists across terminals) it silently redirects every command, so set them explicitly in the command.

## 4. Configuration and the key

```bash
cp .env.example .env
alembic upgrade head
```

`.env` is git-ignored (`.gitignore` lists `.env` and `.env.*`). Every value has a working default; nothing is needed just to
run the tests or the demo. For a real run you need two things:

- **The key**, as `DOCINTEL_ANTHROPIC_API_KEY` or the SDK's own `ANTHROPIC_API_KEY`, in `.env` or in the process environment (the first wins if both are set). Never commit it,
  never paste it into an issue or a log. If you start the app with an inherited `ANTHROPIC_API_KEY` you did not intend to use,
  remove it for that command (`env -u ANTHROPIC_API_KEY ...`) and rely on `.env`.
- **The model**: `DOCINTEL_EXTRACTION_MODEL=claude-haiku-4-5`. A misspelling here fails at the first request, not at start-up.

Settings worth knowing: `DOCINTEL_EXTRACTION_TIMEOUT_SECONDS` (120), `DOCINTEL_EXTRACTION_MAX_RETRIES` (2; **0 for a counted
evaluation run**), `DOCINTEL_MAX_PAGES` (20), `DOCINTEL_MAX_IMAGE_BYTES_TOTAL` (20 MiB), `DOCINTEL_CONFIDENCE_THRESHOLD` (0.85)
and `DOCINTEL_PRICING_USD_PER_MTOK` (add a price for a model not in the built-in table). All are in `.env.example`.

## 5. Run the app

```bash
uvicorn app.main:app --reload
curl localhost:8000/health
```

Upload (`POST /api/v1/documents`), poll `GET /api/v1/documents/{id}` until `page_count` appears, trigger
`POST /api/v1/documents/{id}/extract`, poll again until the status is `extracted`, `needs_review` or `failed` (a failed
document carries the reason in `error`), then review at `http://localhost:8000/review` and export. With no key configured an
extraction fails with "No Anthropic credential is configured", stored on the document and shown on the review page; nothing
is sent anywhere.

## 6. Run the evaluations

```bash
python -m evals.run --dataset invoices_v1                    # offline stub: measures the harness, not extraction
DOCINTEL_EXTRACTION_MAX_RETRIES=0 \
  python -m evals.run --dataset invoices_v1 --provider anthropic --request-cap 80 --max-cost-usd 2.00
```

A real run refuses to start without both caps, with retries on, or for a model with no known price. The caps are for the
whole piece of work (a ledger file, `var/eval_ledger.json`), a request is counted before it is sent, and the first failure
that means stop ends the run with exit code 3 and no result. The plan, the order and the decision rule for the first real runs
are in [EVAL.md](EVAL.md). Point an evaluation at its own database and storage so its documents stay out of your working data:

```bash
createdb -O docintel docintel_evals
DOCINTEL_DATABASE_URL=postgresql+psycopg://docintel:docintel@localhost:5432/docintel_evals \
DOCINTEL_STORAGE_DIR=./var/eval-storage python -m evals.run --dataset invoices_v1
```

## 7. Docker (the app, not just the demo)

```bash
docker compose up --build -d db migrate app      # the app on :8000
docker build -t docintel . && docker run -p 8000:8000 \
  -e DOCINTEL_DATABASE_URL=... -e DOCINTEL_ANTHROPIC_API_KEY="$KEY" -v docintel-storage:/var/lib/docintel/storage docintel
```

The bare image does not run migrations: run `alembic upgrade head` as its own step, as the Compose `migrate` service does.

## 8. Problems you may meet

| Symptom | Cause | Fix |
|---|---|---|
| The demo says "does not recognise this document" | The sample PDFs were line-ending converted by Git on checkout (Git for Windows with `core.autocrlf=true`), changing the bytes the demo hashes | `.gitattributes` now marks PDFs and images binary; re-checkout them (delete the files and `git checkout -- evals/datasets`). `tests/test_fixture_bytes.py` fails if it happens |
| `CERTIFICATE_VERIFY_FAILED` on `pip install` or a Docker build | An antivirus or proxy re-signs HTTPS with a root Python/Docker does not trust | Turn the scanning off while you build, or point `SSL_CERT_FILE` at a bundle that includes your OS roots. Do not turn verification off |
| Commands act on the wrong database | `DOCINTEL_DATABASE_URL` is set in your user environment | Set it explicitly in the command, or unset it |
| An evaluation stops with exit code 3 | The first failure that means stop (no credit, 429, 529, a timeout...) | Read the message; fix the cause; start a new piece of work with a new ledger on purpose. Nothing was retried |
