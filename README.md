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
```

Create the databases (the second one is for the test suite, which migrates and
tears it down on every run):

```bash
createuser docintel --pwprompt
createdb -O docintel docintel
createdb -O docintel docintel_test
```

Apply migrations, then run:

```bash
alembic upgrade head
uvicorn app.main:app --reload
curl localhost:8000/api/v1/health
pytest
```

### Uploading a document

```bash
curl -F file=@invoice.pdf -F doc_type=invoice localhost:8000/api/v1/documents
```

Accepts PDF, PNG, JPEG, TIFF and WEBP. The declared content type is treated as
a claim and verified against the file's own leading bytes, so a text file named
`invoice.pdf` is rejected (415) rather than failing later in the renderer.

The response returns immediately with the document id and `status: uploaded`;
pages are rendered in a `BackgroundTasks` job (the README non-goals rule out a
worker queue). Storage layout, under `DOCINTEL_STORAGE_DIR`:

```
documents/<document_id>/source.pdf
documents/<document_id>/pages/page-0001.png
```

`Document.storage_path` holds the path *relative* to the storage root, so the
root can move without rewriting rows. The stored filename comes from the
detected type, never from the client, so an upload named `../../etc/passwd`
cannot escape the root; the original name is kept in the `filename` column.

Status through this leg of the pipeline:

| Status | Meaning |
| --- | --- |
| `uploaded` | bytes are on disk, nothing rendered yet |
| `processing` | pages are rendering, or are rendered and waiting on the extractor |
| `failed` | rendering gave up; `error` holds the reason and the source is kept |

A successfully rendered document stays `processing` on purpose — nothing has
been extracted yet, and `extracted` would be a lie. Milestone 4 moves it on.

The `error` column is an addition to the README data model, so a failure is
never silent (see "notes for the implementer").

### Extracting a document

```bash
curl -X POST localhost:8000/api/v1/documents/<document_id>/extract
```

Returns 202; the extraction runs in the background against the document's
rendered pages. 404 if the document is unknown, 409 if its pages are not
rendered yet, 422 if no extractor is registered for its `doc_type`.

The model call is reached only through `app/providers/` —
`extract(images, schema, prompt) -> RawExtraction`. `app/providers/
anthropic_vision.py` is the only module that imports the Anthropic SDK;
swapping providers means adding a module there and changing `get_provider()`,
and nothing above that boundary changes.

What a document type contributes lives in `app/extractors/<type>.py`: its
Pydantic schema, its prompt, and a `prompt_version`. Adding a type is a new
module plus one registry entry — the pipeline never names a type.

Every schema field is an `ExtractedField`, so a value always arrives with the
model's own confidence and the page it came from. That confidence is kept
verbatim in `field_values.model_confidence` and is **not** trusted on its own —
see "Validation and confidence" below.

Amounts travel as decimal strings and are parsed to `Decimal`. A JSON number
would arrive as a float and `1234.56` would stop being exact, which milestone
5's totals arithmetic cannot afford.

Every attempt writes an `extractions` row, successful or not:

| Outcome | Row | Document |
| --- | --- | --- |
| Parsed | `raw_response` + `parsed` + one `field_values` row per field | `extracted` |
| Unparseable answer | `raw_response` + `error`, `parsed` null | `failed` |
| Provider error or refusal | `error`, no field values | `failed` |

`raw_response` is kept verbatim and never discarded — it is what you read when
an extraction is wrong, and what future eval runs re-score.

### Validation and confidence

Deterministic checks live in `app/validation/`, one module per document type,
resolved by `doc_type` the same way extractors are. Nothing is asked of the
model that Python can settle:

| Check | Kind | Fields |
| --- | --- | --- |
| `<field>.not_blank` | schema | invoice_number, vendor_name, currency |
| `invoice_date.is_a_date`, `due_date.is_a_date` | schema | that date |
| `dates.due_on_or_after_invoice` | schema | invoice_date, due_date |
| `currency.iso_4217` | schema | currency |
| `totals.subtotal_plus_tax_equals_total` | arithmetic | subtotal, tax, total |
| `line_items.sum_to_subtotal` | arithmetic | line_items, subtotal |
| `line_items.row_quantity_times_price` | arithmetic | line_items |

Every check is `passed`, `failed`, or **`skipped`** — the third is the important
one. An invoice that states no subtotal has not got its arithmetic wrong, so
the check is skipped and that signal simply does not apply to those fields.

The final score combines the README's four signals:

```
confidence = Σ(weight_i × score_i) / Σ(weight_i)   over applicable signals only
```

| Signal | Default weight | Score | Applies when |
| --- | --- | --- | --- |
| model | 0.40 | the model's own number | always |
| schema | 0.20 | 1 passed / 0 failed | a schema check ran for the field |
| arithmetic | 0.20 | 1 passed / 0 failed | an arithmetic check ran for the field |
| text_layer | 0.20 | 1 found / 0 not found | the document has a text layer and the value is a scalar |

Weights need not sum to 1 — inapplicable signals drop out and the rest are
renormalised, so a field is never marked down for a check that could not run.
The defaults give the model under half the say, so the deterministic signals
together outweigh it wherever they apply. All five values are configurable
(`DOCINTEL_CONFIDENCE_THRESHOLD`, `DOCINTEL_CONFIDENCE_WEIGHT_*`); none is
hard-coded in the scoring logic.

`needs_review` is set when **the score is below the threshold (default 0.85),
or any deterministic check for that field failed**. The second clause matters:
a failed check means the value is known to be wrong, and no weighting should
let a confident model hide that. A missing string in the text layer is *not* a
blocking failure — text layers are reformatted and imperfect, so it lowers the
score but never forces review on its own.

`field_values` therefore carries three numbers: `model_confidence` (verbatim,
never overwritten), `confidence` (the final score), and `validation` (the
signal-by-signal breakdown, so a flagged field can say why). The gap between
the first two is how a confidently wrong model gets caught.

A document with any flagged field lands in `needs_review`; otherwise
`extracted`.

**Page text layer.** `rendering.extract_text_layer` reads the text already
embedded in the source PDF. A scan, a photo, or an image upload has none, and
that returns an empty text layer — meaning "this signal does not apply", never
"the value is wrong". Matching ignores case, spacing, currency symbols and
thousands separators, so `1,210.00` on the page corroborates `1210.00`. The
whole document is searched rather than only the page the model named: a
mis-numbered `source_page` is a separate problem and should not fail a value
that is plainly in the document.

### Reviewing flagged fields

```bash
curl localhost:8000/api/v1/review          # JSON queue
open localhost:8000/review                 # the reviewer's page
```

`GET /api/v1/review` returns only field values whose persisted `needs_review`
is true, least confident first, with a stable tie-break so the same queue pages
the same way twice (`limit`, default 50, max 200; `offset`). Each entry carries
the document (filename, type, status, page count), the field name and extracted
value, `confidence` (the final score) alongside `model_confidence` (what the
model claimed), `source_page`, the failed deterministic checks, the full signal
breakdown, and a `review_reason` sentence. The response echoes the current
`confidence_threshold`.

**The queue never rescores.** `needs_review`, `confidence` and `validation` are
written once, when the extraction runs. Changing the threshold afterwards does
not silently re-sort the queue — the rows record the decision that was actually
made, and a re-extraction is what changes them.

A field whose text-layer signal is simply *absent* — a scan, a photo, or a value
that cannot be looked for — reports `text_layer_miss: false` and says nothing
about the text layer in its reason. Only a signal that ran and came back empty
counts as a miss.

`GET /review` renders the same rows as a plain Jinja2 table, grouped by
document: no build step, no framework, no stylesheet beyond a few rules inline.
It is read-only in this milestone.

`GET /review` also carries a one-line correction form per queued field and a
"Recently corrected" table, so a reviewer can see their work was recorded. The
form posts to `POST /review/{field_id}` (form-encoded, then a 303 back to the
page, so a reload does not resubmit); the JSON API keeps its own content type.

### Correcting a field

```bash
curl -X POST localhost:8000/api/v1/review/<field_id> \
     -H 'content-type: application/json' \
     -d '{"corrected_value": "1210.00"}'
```

A correction is ground truth. The model is **not** called again, and the field
is not rescored. What changes:

| Column | After a correction |
| --- | --- |
| `field_values.value` | the corrected value — this is what the export reads |
| `field_values.needs_review` | false; the field has been handled |
| `corrections` | a new row: `original_value` (what it said a moment ago), `corrected_value`, `corrected_at` |

What deliberately does not change: `model_confidence`, `confidence`,
`validation`, and `extractions.raw_response`. All four describe the *model's*
answer and stay true of it forever. Read them next to `FieldValue.is_corrected`
— derived from the corrections table, so the two can never drift — which says
whether the current value is still the one they describe.

Correcting a field twice writes two rows whose values chain back to the model's
original, so no revision is overwritten. An empty corrected value is a valid
correction: it means the document does not state this field.

Document status is untouched by *reading* the queue. A correction resolves it:
the document stays `needs_review` while any field still is, and becomes
`reviewed` once none is. A `failed` or still-processing document is never moved
by a correction.

### Exporting the record

```bash
curl localhost:8000/api/v1/documents/<id>/export             # json (default)
curl localhost:8000/api/v1/documents/<id>/export?format=csv
```

Exactly two formats; anything else is a 422, an unknown document a 404, and a
document with no completed extraction a 409. Raw model responses are never
exported — they stay in `extractions.raw_response` for debugging and evals.

**JSON.** Three top-level keys. `original_value` and `corrected_at` appear only
on fields a human changed:

```json
{
  "document":   {"id", "filename", "doc_type", "status", "page_count", "uploaded_at"},
  "extraction": {"id", "model_name", "prompt_version", "extracted_at"},
  "fields": {
    "total": {
      "value": "1210.00",
      "corrected": true,
      "source_page": 2,
      "needs_review": false,
      "confidence": 0.49,
      "model_confidence": 0.98,
      "original_value": "1500.00",
      "corrected_at": "2026-09-11T07:16:52+00:00"
    },
    "line_items": {"value": [{"description": "...", "quantity": "10",
                              "unit_price": "40.00", "amount": "400.00"}], "...": "..."}
  }
}
```

`line_items` comes back as a nested list, not a JSON string — which field is
structured is read off the extractor's schema, not guessed from the stored text.

**CSV.** One row per field, twelve columns:

```
document_id,filename,doc_type,document_status,field_name,value,corrected,
original_value,confidence,model_confidence,source_page,needs_review
```

A null is an empty cell and `line_items` is written as compact JSON in its
cell — a standard parseable representation rather than a Python `repr`.

### Cost

`input_tokens`, `output_tokens`, `latency_ms` and `cost_usd` are recorded per
extraction. Cost is computed from a published price table in the Anthropic
provider; a model missing from that table stores `cost_usd` as null rather
than a guess.

### Running an evaluation

```bash
python -m evals.run --dataset invoices_v1                    # real API, costs money
python -m evals.run --dataset invoices_v1 --provider stub    # offline, harness only
python -m evals.run --dataset invoices_v1 --limit 3 --json --no-persist
```

`--provider anthropic` is the default and is the only setting whose numbers
mean anything. `--provider stub` answers every document identically without a
network call; it exists to exercise the harness in CI and prints a banner
saying its numbers are not a measurement of extraction quality.

Every document goes through the real pipeline — store, render, extract,
validate, score — so what is measured is the system, not a shortcut through it.
Eval documents are written to the configured database and storage like any
other upload; they are the evidence behind the numbers, so they are kept.

The run exits non-zero if any document failed to extract. A failed document's
labelled fields are counted as **wrong**, never skipped, and cannot be scored
correct even where the label is null — the pipeline did not answer "absent", it
did not answer at all.

**Dataset.** `evals/datasets/invoices_v1/` holds 20 synthetic invoices and
`labels.json`; see that directory's README. Labels are the values used to render
each PDF, so they describe what the document says, independently of extraction.

**Metric definitions.**

| Metric | Definition |
| --- | --- |
| exact-match accuracy | per field: matching values / labelled occurrences of that field |
| review rate | flagged fields / evaluated fields |
| document review rate | documents with ≥1 flagged field / documents |
| **false-confident rate** | wrong **and not flagged** / evaluated fields |
| ...of unflagged fields | wrong and not flagged / not-flagged fields |
| mean cost | sum of `extractions.cost_usd` / documents that reported a cost |
| latency | `extractions.latency_ms`: the provider call alone, request sent to full response received. Excludes rendering, scoring and database writes. |
| p50 / p95 | nearest-rank, so the figure is always a latency actually observed |

The report prints all four outcomes — correct/flagged, correct/unflagged,
wrong/flagged (caught), wrong/unflagged (false-confident) — so "wrong" is never
confused with "wrong and nobody was told".

**Normalisation** removes presentation only: money compares as decimals
(`1,210.00` == `1210.0`), line items compare structurally row by row, and other
values have whitespace collapsed. Case and punctuation are **not** normalised —
`ACME` and `Acme`, `INV20261001` and `INV-2026-1001`, are different answers. An
absent value matches only another absent value.

Every run writes an `eval_runs` row: dataset, prompt version, model, document
count, per-field accuracy, review rate, false-confident rate, mean cost, mean
latency, and a `metrics` blob holding the percentiles, outcome counts and any
failures. Bump `prompt_version` on every prompt change so the before/after
number is always attached to it.

### Migrations

Alembic reads the database URL from `DOCINTEL_DATABASE_URL` via `app/config.py`
— `alembic.ini` deliberately holds no URL, so migrations and the app can never
disagree about which database they are using.

```bash
alembic upgrade head                                  # apply
alembic downgrade base                                # roll back to empty
alembic current                                       # which revision is applied
alembic check                                         # models vs. database: any drift?
alembic revision --autogenerate -m "add extractions"  # new migration
```

Every model must be imported in `app/models/__init__.py`; autogenerate only
sees tables attached to `Base.metadata`. Review autogenerated migrations before
committing them — in particular, Postgres `ENUM` types outlive the table that
uses them, so a downgrade has to drop the type explicitly (see the first
migration for the pattern).

Tests that need a database are skipped when no server answers, so `pytest` still
runs without PostgreSQL installed.

Configuration is environment-only (`DOCINTEL_*`, see `.env.example`); there are
no settings hard-coded outside `app/config.py`. Dependencies are added in the
milestone that first imports them, so an install never pulls in a library the
code does not yet use.
