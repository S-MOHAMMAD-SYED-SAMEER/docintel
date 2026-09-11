# DocIntel — Document Intelligence with Human Review

Extract structured data from business documents (invoices, purchase orders, contracts) into a validated schema, score confidence per field, and route anything uncertain to a human review queue.

**Why this exists:** small firms re-key invoice data by hand. A model that guesses silently is worse than useless — the value is in knowing which fields to trust.

## v1 Scope

- Upload PDF or image → extraction job
- Extract to a strict schema (Pydantic), per-field confidence
- Any field below threshold → review queue
- Reviewer corrects field → correction stored as ground truth
- Export corrected record as JSON / CSV
- Eval harness over a labelled set, run from CLI

## Non-goals (do not build)

- Auth/multi-tenancy (single local user)
- Fine-tuning or training a custom model
- A polished design system — plain functional UI only
- Handwriting, multi-language, or table-of-tables extraction
- Background worker queues (Celery/Redis). Use FastAPI `BackgroundTasks`.

## Stack

- Python 3.13, FastAPI, Uvicorn
- Pydantic v2 + pydantic-settings
- SQLAlchemy 2.x, Alembic, psycopg 3, PostgreSQL (`postgresql+psycopg://`)
- `pypdfium2` for rendering PDF pages to images
- Anthropic SDK (vision) behind a provider interface — no hard-coded provider calls in route handlers
- pytest, HTTPX
- UI: server-rendered Jinja2 templates. No React in v1.

## Architecture

```
upload → storage → page render → extractor (LLM vision)
       → schema validation → confidence scoring
       → [pass] extracted record
       → [fail] review queue → human correction → record
```

Rules:

- `app/extractors/` holds one module per document type. Adding a type must not require editing the pipeline.
- `app/providers/` abstracts the model call: `extract(images, schema, prompt) -> RawExtraction`.
- Deterministic checks (date parsing, totals arithmetic, currency codes, line-items summing to total) live in `app/validation/` — not in the prompt. If the maths can be checked in Python, check it in Python.

## Data model

- **documents** — id, filename, storage_path, doc_type, page_count, status (`uploaded|processing|extracted|needs_review|reviewed|failed`), uploaded_at
- **extractions** — id, document_id FK, model_name, prompt_version, raw_response (JSONB), parsed (JSONB), input_tokens, output_tokens, cost_usd, latency_ms, created_at
- **field_values** — id, extraction_id FK, field_name, value (text), confidence (float), source_page, needs_review (bool)
- **corrections** — id, field_value_id FK, original_value, corrected_value, corrected_at
- **eval_runs** — id, dataset_name, prompt_version, model_name, field_accuracy (JSONB), mean_cost_usd, mean_latency_ms, created_at

## Confidence scoring

Do not ask the model for a 0–1 confidence and trust it. Combine:

1. Self-reported confidence from the model (asked per field)
2. Schema validation pass/fail (type, format, enum)
3. Arithmetic consistency (line items sum to subtotal; subtotal + tax = total)
4. Presence of the extracted string in the page text layer, where a text layer exists

Final score = weighted combination, weights in config. Threshold for review is configurable, default 0.85.

## API

```
POST   /api/v1/documents            upload, returns document_id
GET    /api/v1/documents/{id}       status + parsed result
GET    /api/v1/review               queue of fields needing review
POST   /api/v1/review/{field_id}    submit a correction
GET    /api/v1/documents/{id}/export?format=json|csv
GET    /api/v1/health
```

## Build milestones

Commit at the end of each. Conventional Commits.

1. FastAPI skeleton, config via Pydantic Settings, `/health`, `.env.example`
2. Postgres + Alembic wired, `documents` table, first migration
3. Upload endpoint, file storage on disk, PDF → page images
4. Provider interface + Anthropic vision call, invoice schema, `extractions` + `field_values` tables
5. Deterministic validation layer + confidence scoring
6. Review queue endpoints + Jinja2 review page
7. Corrections stored; export endpoint
8. Eval harness: `python -m evals.run --dataset invoices_v1` prints per-field accuracy, cost, latency
9. Second doc type (purchase order) added without touching pipeline code — this proves the abstraction
10. README results section, Dockerfile, deploy notes

## Evals (required, not optional)

`evals/datasets/invoices_v1/` — 20 documents + `labels.json` with expected field values.

Metrics: exact-match accuracy per field, review-queue rate, false-confident rate (high confidence + wrong — the metric that matters most), mean cost, p50/p95 latency.

Every prompt change bumps `prompt_version` and gets a new eval run row. Never change a prompt without a before/after number.

**Acceptance for v1:** ≥90% exact-match on total, invoice number, and date; false-confident rate below 5%.

## Cost tracking

Log `input_tokens`, `output_tokens`, `cost_usd` on every extraction. README must state cost per document. Use a cheaper model tier for simple, text-layer-present documents and escalate to a stronger model only when validation fails — implement this as a routing step in milestone 8 and report the savings.

## Notes for the implementer

- Store raw model responses. Never discard them; they are needed for debugging and future evals.
- Failed extractions must fail loudly with a stored error, not silently return empty fields.
- Small, reviewable changes. Do not swap frameworks, add a queue system, or introduce a frontend build step without being asked.

## Running locally

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # edit as needed
uvicorn app.main:app --reload
curl localhost:8000/api/v1/health
pytest
```

Configuration is environment-only (`DOCINTEL_*`, see `.env.example`); there are
no settings hard-coded outside `app/config.py`. Dependencies are added in the
milestone that first imports them, so an install never pulls in a library the
code does not yet use.
