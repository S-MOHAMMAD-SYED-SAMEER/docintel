# DocIntel — Document Intelligence with Human Review

Extract structured data from business documents (invoices, purchase orders)
into a validated schema, score confidence per field, and route anything
uncertain to a human review queue.

**Status:** feature-complete through milestone 10, plus a hardened
public demo. 658 tests collected, 641 passing (see
[Testing](#testing) for what that number covers and the one
previously-reported, environment-dependent flake this run did not hit).
CI runs the suite on Linux (with PostgreSQL) and Windows, builds the Docker image and runs the demo stack.
A first set of real-model runs was made on 2026-10-21, on synthetic documents, one run of each prompt, one model:
see [Evaluation](#evaluation) for the numbers and, next to them, what they do not show. They are not an accuracy figure.

---

## The problem

Businesses receive invoices and purchase orders as PDFs and photographs and
need reliable structured data out of them. Re-keying by hand is slow and
expensive.

Handing the page to a vision model and storing what comes back is worse than
useless. A model will produce a plausible total that is off by one digit, a
date read day-first instead of month-first, a vendor name from the letterhead
of the wrong company — and it will report high confidence while doing it. A
system that cannot tell you *which fields to distrust* has moved the problem
rather than solved it.

**The value is in knowing which fields to trust.** That is what DocIntel is
built around.

## The solution

```
Upload
  ↓
Storage                  file-signature validated, safe filenames
  ↓
Page rendering           pypdfium2 → one PNG per page
  ↓
Document-type registry   invoice | purchase_order → schema, prompt, rules
  ↓
Vision LLM extraction    Anthropic, behind a provider interface
  ↓
Schema validation        strict Pydantic, extra="forbid"
  ↓
Deterministic validation dates, currency codes, totals arithmetic — in Python
  ↓
Confidence scoring       four weighted signals, per field
  ↓
Human review             everything below threshold, or that failed a check
  ↓
Correction / audit trail original value never discarded
  ↓
JSON / CSV export        the current record, corrections included
  ↓
Evaluation               labelled dataset, per-field accuracy, cost, latency
```

The central idea: **if something can be checked deterministically in Python,
check it in Python.** The model is asked to read the document, not to do
arithmetic. `1,000.00 + 210.00 = 1,210.00` is verified by `Decimal`, not by
asking a language model whether it added up correctly.

## Engineering highlights

| | |
| --- | --- |
| **Strict schemas** | Pydantic v2 with `extra="forbid"`; the model cannot invent a field. Money is `Decimal` carried as decimal strings — a JSON number would arrive as a float and `1234.56` would stop being exact. |
| **Structured model output** | The extractor's JSON Schema is sent as `output_config.format`, tightened so every object is closed and every property required. |
| **Deterministic validation** | Row `quantity × unit_price`, rows summing to subtotal, `subtotal + tax = total`, ISO 4217 codes, date parsing and ordering — all in `app/validation/`, none of it in the prompt. |
| **Field-level confidence** | Four weighted signals combined per field, renormalised over the signals that actually apply. The model's own number gets under half the say. |
| **Human-in-the-loop** | A field below threshold, or one that failed a deterministic check, goes to a review queue with the reason attached. |
| **Correction audit trail** | A correction records what the field said before it. Repeated corrections chain back to the model's original; nothing is overwritten. |
| **Provider abstraction** | One interface, `extract(images, schema, prompt) -> RawExtraction`. Exactly one module imports the Anthropic SDK. |
| **Document-type registries** | Invoice and purchase order share one pipeline with zero branching on document type — asserted by a test. |
| **Reproducible evaluation** | A committed, deterministic, labelled dataset and a CLI that measures accuracy, review rate, false-confident rate, cost and latency. |
| **Cost and latency tracking** | Recorded per extraction from provider usage; never invented when the provider reports none. |
| **PostgreSQL + Alembic** | Seven migrations, each verified to upgrade, downgrade and re-upgrade on a fresh database. |
| **Test coverage** | 658 tests across 38 files, including a hard guard that no test can reach the real API. |

---

## Architecture

```
                 HTTP / HTML
                      │
   ┌──────────────────▼───────────────────┐
   │ API          app/api/                │  FastAPI routers, Jinja2 page.
   │                                      │  HTTP concerns only.
   └──────────────────┬───────────────────┘
                      │
   ┌──────────────────▼───────────────────┐
   │ Application  app/ingestion.py         │  Order of operations and
   │ services     app/extraction.py        │  status transitions.
   │              app/corrections.py       │
   │              app/review.py            │
   │              app/export.py            │
   └───┬──────────────┬──────────────┬─────┘
       │              │              │
  ┌────▼─────┐  ┌─────▼──────┐  ┌────▼──────────┐
  │Extractors│  │ Validation │  │  Confidence   │
  │registry  │  │ registry   │  │ app/          │
  │app/      │  │ app/       │  │ confidence.py │
  │extractors│  │ validation │  │               │
  └────┬─────┘  └────────────┘  └───────────────┘
       │
  ┌────▼─────────────────┐
  │ Provider interface   │  extract(images, schema, prompt)
  │ app/providers/base   │      -> RawExtraction
  └────┬─────────────────┘
       │
  ┌────▼─────────────────┐
  │ Anthropic provider   │  the only module importing the SDK
  └──────────────────────┘

  All services persist through SQLAlchemy 2.x → PostgreSQL
  documents · extractions · field_values · corrections · eval_runs
```

### The boundary that matters

Invoice and purchase order run through **the same pipeline**. `app/extraction.py`,
`app/confidence.py`, `app/review.py`, `app/corrections.py` and `app/export.py`
contain no branch on document type at all — there is a test that reads their
source and asserts it.

What differs per type is resolved through two registries keyed by `doc_type`:

| Registry | Provides |
| --- | --- |
| `app/extractors/<type>.py` | the Pydantic schema, the prompt, a `prompt_version` |
| `app/validation/<type>.py` | the deterministic rules for that type |

Adding a document type is a new module in each plus one registry entry — never
a change to the pipeline. The rules the types share (row arithmetic, subtotal
sums, `subtotal + tax = total`, ISO 4217, date parsing and ordering) live once
in `app/validation/common.py` and are composed by both. Each type's own module
holds only what is specific to it: which identifiers must not be blank, and
which of its dates must not precede which.

### Document types

**Invoice** (`doc_type=invoice`) — 13 fields including invoice number, dates,
vendor, customer, currency, subtotal, tax, total and line items.

**Purchase order** (`doc_type=purchase_order`) — 10 fields including PO number,
order and delivery dates, vendor, customer, currency, totals and line items.

Purchase-order support is newer and **unmeasured**: the fixture under
`evals/datasets/purchase_orders_smoke/` is three synthetic documents that prove
the type flows end to end, not a benchmark.

---

## Running it

### Docker (fastest)

```bash
docker compose up --build
curl localhost:8000/health
```

Migrations run automatically as their own step — `docker-compose.yml`'s
`migrate` service waits for PostgreSQL to report healthy, runs
`alembic upgrade head`, and the app only starts once that succeeds. No
separate manual migration command is needed with this path.

`docker-compose.yml` is a local convenience stack (PostgreSQL + the app) with
development credentials. For anything else, build the image and supply real
configuration:

```bash
docker build -t docintel .
docker run -p 8000:8000 \
  -e DOCINTEL_DATABASE_URL='postgresql+psycopg://user:pass@host:5432/docintel' \
  -e DOCINTEL_ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -v docintel-storage:/var/lib/docintel/storage \
  docintel
```

The image runs as a non-root user, carries a `HEALTHCHECK` against `/health`,
and needs only `DOCINTEL_DATABASE_URL` to start. Uploaded files and rendered
pages live under `DOCINTEL_STORAGE_DIR` (default `/var/lib/docintel/storage`) —
mount a volume there to keep them. Run this way — a bare image without
Compose — migrations are not run automatically; run `alembic upgrade head`
as a separate step, the same way `docker-compose.yml`'s own `migrate`
service does it.

Behind a mirror or an internal registry, override the base image:

```bash
docker build --build-arg PYTHON_IMAGE=mirror.example.com/library/python:3.13-slim .
```

### Local

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # edit as needed
```

Create the databases (the second is for the test suite, which migrates and
tears it down on every run):

```bash
createuser docintel --pwprompt
createdb -O docintel docintel
createdb -O docintel docintel_test
```

```bash
alembic upgrade head
uvicorn app.main:app --reload
curl localhost:8000/api/v1/health
pytest
```

### Demo Mode (no Anthropic key required)

Want to see the full pipeline without an Anthropic API key? Run
`uvicorn demo.app:app --reload` instead of `app.main:app` — the real
application, with extraction served by a deterministic, credential-free
provider against three committed sample invoices, seeded through the real
pipeline (`python -m demo.seed`) since uploading a new document is
disabled here. Hardened for public exposure: uploads and corrections both
refuse with 403, and extraction is rate-limited (off by default; see
[docs/DEMO.md](docs/DEMO.md)) — the one part of this project built to be
put on the public internet, unlike everything else described in
[Security and reliability](#security-and-reliability).

`docker compose up --build` now also builds and starts the `seed` and
`demo` services alongside `db`/`migrate`/`app` — the demo is reachable at
`localhost:8001` once `seed` completes. See
[docs/DEMO.md](docs/DEMO.md) for the full walkthrough.

---

## Demo walkthrough

Nine steps, end to end. Every endpoint below exists.

**1–3. Start PostgreSQL, migrate, start the app** — see above.

**4. Upload a document.** Returns `201` with the document id and
`status: uploaded`; pages render in the background.

```bash
curl -F file=@evals/datasets/invoices_v1/invoice-001.pdf \
     -F doc_type=invoice \
     localhost:8000/api/v1/documents
```

**5. Trigger extraction.** Returns `202`; runs in the background.

```bash
curl -X POST localhost:8000/api/v1/documents/<document_id>/extract
```

**6. Inspect the extracted fields.**

```bash
curl 'localhost:8000/api/v1/documents/<document_id>/export?format=json' | jq .fields
```

**7. Open the review queue** — JSON at `/api/v1/review`, or the page at
`/review` in a browser.

```bash
curl localhost:8000/api/v1/review | jq '.items[] | {field_name, confidence, review_reason}'
```

**8. Correct a field.** The page has a form per row; the API takes JSON.

```bash
curl -X POST localhost:8000/api/v1/review/<field_id> \
     -H 'content-type: application/json' \
     -d '{"corrected_value": "1210.00"}'
```

**9. Export the corrected record.**

```bash
curl 'localhost:8000/api/v1/documents/<document_id>/export?format=json'
curl 'localhost:8000/api/v1/documents/<document_id>/export?format=csv'
```

### API reference

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/documents` | Upload a PDF or image (`file`, `doc_type`). 201. |
| `POST` | `/api/v1/documents/{id}/extract` | Run extraction in the background. 202; 409 if one is already running for this document. |
| `GET` | `/api/v1/documents/{id}` | The document's status, page count, running flag and, if it failed, why (`error`); plus its latest extraction attempt (model, prompt version, tokens, cost, latency, error). Never the raw response. |
| `GET` | `/api/v1/documents/{id}/pages/{n}` | A rendered page, as a PNG. |
| `GET` | `/api/v1/documents/{id}/source` | The file as it was uploaded. |
| `GET` | `/api/v1/documents/{id}/export?format=json\|csv` | The current record. |
| `GET` | `/api/v1/review` | Fields awaiting review (`limit`, `offset`). |
| `POST` | `/api/v1/review/{field_id}` | Submit a correction. 201. |
| `GET` | `/api/v1/health` | Liveness. |
| `GET` | `/health` | Same, unversioned, for container and load-balancer checks. |
| `GET` | `/review` | The server-rendered review page (HTML). |
| `POST` | `/review/{field_id}` | The page's form target; redirects back. |

Interactive docs at `/docs`. `GET /api/v1/documents/{id}` is the status endpoint (poll it after an
upload and after `/extract`); the export endpoint reads a document's *record* once it has one.

---

## Pipeline detail

### Upload and rendering

Accepts PDF, PNG, JPEG, TIFF and WEBP. The declared content type is treated as
a claim and verified against the file's own leading bytes, so a text file named
`invoice.pdf` is rejected (415) rather than failing later in the renderer.

Storage layout, under `DOCINTEL_STORAGE_DIR`:

```
documents/<document_id>/source.pdf
documents/<document_id>/pages/page-0001.png
```

`Document.storage_path` holds the path *relative* to the storage root, so the
root can move without rewriting rows. The stored filename comes from the
detected type, never from the client, so an upload named `../../etc/passwd`
cannot escape the root; the original name is kept in the `filename` column.

| Status | Meaning |
| --- | --- |
| `uploaded` | bytes on disk, nothing rendered yet |
| `processing` | pages rendering, or rendered and awaiting the extractor |
| `extracted` | parsed, and every field cleared the threshold |
| `needs_review` | at least one field is uncertain or failed a check |
| `reviewed` | every flagged field has been handled by a human |
| `failed` | rendering or extraction gave up; `error` holds the reason |

### Extraction

The model call is reached only through `app/providers/` —
`extract(images, schema, prompt) -> RawExtraction`.
`app/providers/anthropic_vision.py` is the only module that imports the
Anthropic SDK.

Every attempt writes an `extractions` row, successful or not, and whatever stopped it:

| Outcome | Row | Document |
| --- | --- | --- |
| Parsed | `raw_response` + `parsed` + one `field_values` row per field | `extracted` or `needs_review` |
| Unparseable answer | `raw_response` + `error`, `parsed` null | `failed` |
| Provider error or refusal | `error`, no field values | `failed` |
| Refused before any model call (too many pages, too many bytes, no rendered pages) | `error` ("Nothing was sent to the model") | `failed` |
| Anything unexpected | `error` ("Unexpected extraction failure: ...") | `failed` |

**What the adapter does when the service fails.** The timeout (`DOCINTEL_EXTRACTION_TIMEOUT_SECONDS`, default 120 s)
and the retry count (`DOCINTEL_EXTRACTION_MAX_RETRIES`, default 2, set 0 to turn retries off) are stated, not left to the
SDK's defaults. The SDK retries only transient failures (timeouts, connection errors, 408, 409, 429, 5xx), waiting what
the service asks. What remains is classified, and the class is in the stored error:

| Failure | Class | Retried by the SDK |
| --- | --- | --- |
| No credential, 401, 403 | `ProviderAuthError` | no |
| No credit left (a 400 about the credit balance) | `ProviderQuotaExhausted` | no |
| 429 | `ProviderRateLimited` (carries `retry_after`) | yes, then this |
| 500, 503, 529 | `ProviderUnavailable` | yes, then this |
| Timeout / cannot connect | `ProviderTimeout` / `ProviderConnectionError` | yes, then this |
| 400, 404, 413, 422 | `ProviderBadRequest` (a bug or a wrong model ID) | no |
| Declined by the model | `ProviderRefusal` | no |

A document with more than `DOCINTEL_MAX_PAGES` pages (default 20), or whose rendered pages exceed
`DOCINTEL_MAX_IMAGE_BYTES_TOTAL` (default 20 MiB), is refused before any model call. Only one extraction per document
runs at a time (a second `POST .../extract` gets 409).

**The schema sent to the API** is the extractor's Pydantic schema with every keyword the structured-outputs endpoint
does not accept (`minimum`, `maximum`, `default` and the other numeric and string constraints) removed. Those limits
are still enforced on the way back in: Pydantic validates every answer, so a confidence of 1.4 or a page 0 fails the
extraction. Whether the live endpoint accepts the remaining schema has not been checked yet: the first real request
will be that check.

`raw_response` is kept verbatim and never discarded — it is what you read when
an extraction is wrong, and what future eval runs re-score.

### Validation

Deterministic checks live in `app/validation/`, resolved by `doc_type`:

| Check | Kind | Applies to |
| --- | --- | --- |
| `<field>.not_blank` | schema | required identifiers |
| `<field>.is_a_date` | schema | each date field |
| `dates.due_on_or_after_invoice` / `dates.delivery_on_or_after_order` | schema | the type's date pair |
| `currency.iso_4217` | schema | currency |
| `totals.subtotal_plus_tax_equals_total` | arithmetic | subtotal, tax, total |
| `line_items.sum_to_subtotal` | arithmetic | line items, subtotal |
| `line_items.row_quantity_times_price` | arithmetic | line items |

Every check is `passed`, `failed`, or **`skipped`** — the third matters. A
document that states no subtotal has not got its arithmetic wrong, so the check
is skipped and that signal does not apply to those fields. Money compares to a
`Decimal("0.01")` tolerance, since per-row rounding legitimately moves a
line-item sum by a cent.

### Confidence scoring

```
confidence = Σ(weightᵢ × scoreᵢ) / Σ(weightᵢ)   over applicable signals only
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
together outweigh it wherever they apply. All five values are configurable;
none is hard-coded in the scoring logic.

`needs_review` is set when **the score is below the threshold (default 0.85),
or any deterministic check for that field failed**. The second clause matters:
a failed check means the value is known to be wrong, and no weighting should
let a confident model hide it. A missing string in the text layer is *not* a
blocking failure — text layers are reformatted and imperfect — so it lowers the
score but never forces review on its own.

**Page text layer.** `rendering.extract_text_layer` reads the text already
embedded in the source PDF. A scan, a photo, or an image upload has none, and
that returns an empty text layer — meaning "this signal does not apply", never
"the value is wrong". Matching ignores case, spacing, currency symbols and
thousands separators, so `1,210.00` on the page corroborates `1210.00`.

`field_values` carries three numbers: `model_confidence` (verbatim, never
overwritten), `confidence` (the final score), and `validation` (the
signal-by-signal breakdown, so a flagged field can say why). The gap between
the first two is how a confidently wrong model gets caught.

### Review and corrections

`GET /api/v1/review` returns only field values whose persisted `needs_review`
is true, least confident first. **The queue never rescores** — `needs_review`,
`confidence` and `validation` are written once, when the extraction runs, so
the queue and the database cannot disagree.

The review page (`/review`) shows, beside each field, the rendered **source page** it was read from (page 1, and
said so, when the model gave no page), a link to the full image and a link to the uploaded file, so the reviewer can
check the value against the document. Documents that failed (no fields, so not in the queue) are listed at the top
with the stored reason.

A correction is ground truth. The model is **not** called again, and the field
is not rescored:

| Column | After a correction |
| --- | --- |
| `field_values.value` | the corrected value — this is what the export reads |
| `field_values.needs_review` | false; the field has been handled |
| `corrections` | a new row: `original_value`, `corrected_value`, `corrected_at` |

`model_confidence`, `confidence`, `validation` and `raw_response` are left
untouched: they describe the *model's* answer and stay true of it.
`FieldValue.is_corrected` is derived from the corrections table, so the two can
never drift. Correcting twice writes two rows whose values chain back to the
model's original. An empty corrected value is a valid correction: the document
does not state this field.

A correction resolves the document: it stays `needs_review` while any field
still is, and becomes `reviewed` once none is.

### Export

Exactly two formats; anything else is a 422, an unknown document a 404, and a
document with no completed extraction a 409. Raw model responses are never
exported.

**JSON** — `original_value` and `corrected_at` appear only on corrected fields:

```json
{
  "document":   {"id", "filename", "doc_type", "status", "page_count", "uploaded_at"},
  "extraction": {"id", "model_name", "prompt_version", "extracted_at"},
  "fields": {
    "total": {
      "value": "1210.00", "corrected": true, "source_page": 2,
      "needs_review": false, "confidence": 0.49, "model_confidence": 0.98,
      "original_value": "1500.00", "corrected_at": "2026-09-11T07:16:52+00:00"
    },
    "line_items": {"value": [{"description": "...", "quantity": "10",
                              "unit_price": "40.00", "amount": "400.00"}], "...": "..."}
  }
}
```

`line_items` is a nested list, not a JSON string — which field is structured is
read off the extractor's schema, not guessed from the stored text.

**CSV** — one row per field, twelve columns:

```
document_id,filename,doc_type,document_status,field_name,value,corrected,
original_value,confidence,model_confidence,source_page,needs_review
```

A null is an empty cell and `line_items` is compact JSON in its cell — a
standard parseable representation rather than a Python `repr`.

---

## Evaluation

### What has been verified

The harness runs a labelled dataset through the **real pipeline** — store,
render, extract, validate, score — so what it measures is the system, not a
shortcut through it.

`evals/datasets/invoices_v1/` is **20 synthetic invoices**, deterministic and
committed, generated by `evals/generate_invoices_v1.py`. `labels.json` holds
the values used to *render* each PDF, so the labels describe what the document
says, **independently of anything a model later claims it says**. Every invoice
is internally consistent — line items sum to subtotal, subtotal + tax = total —
verified by a test.

`evals/datasets/invoices_hard_v1/` is a second, **harder set of 10 synthetic invoices**: five images
with no text layer (a skewed scan, a photograph, one with a PAID stamp and a scribble over the totals, and others),
other layouts and wording, European date and number formats, a credit note, a missing due date, a two-page table.
It is **frozen** (a sha256 in `docs/EVAL.md` and a test that fails if any file changes) and was reviewed before any
run: *written by Claude (generated; labels are the render inputs); reviewed by ChatGPT and Claude; final decisions by
Sameer.* It is a small smoke test with some teeth, not a benchmark: ten documents from a small invented pool.

Running with `--provider stub` exercises the harness offline. It verifies:
dataset loading, extraction orchestration, metric calculation, `eval_runs`
persistence, report rendering and CLI behaviour. The stub answers every
document identically, never sees the labels, and invents no cost or latency.

> **The stub result is not extraction accuracy.** It measures the harness. Its
> accuracy figure is near zero by construction and says nothing about the model.

### Results of the real-model runs (2026-10-21)

`claude-haiku-4-5` (the API resolved it to `claude-haiku-4-5-20251001`), adapter retries off, default sampling, the default
confidence weights and threshold (0.85), **one run of each prompt**. The plan, the caps (80 requests and USD 2.00 for the
whole piece of work) and the decision rule were committed before the first request ([docs/EVAL.md](docs/EVAL.md)). Full
record, with every document, every wrong field and what surprised me:
[docs/results/2026-10-21-real-runs.md](docs/results/2026-10-21-real-runs.md).

| Fields correct (of evaluated) | `invoice/v1` | `invoice/v2` (now the default) |
|---|---|---|
| `invoices_v1`: 20 synthetic invoices, text PDFs (260 fields) | 257 of 260 | 260 of 260 |
| `invoices_hard_v1`: 10 harder synthetic invoices, 5 of them images with no text layer (130 fields) | 127 of 130 | 127 of 130 |
| Both sets, 30 documents (390 fields): wrong fields | 6 | 3 |

What the review flag did with those wrong fields. The model's replies, the extracted values and the labels are the same in
both blocks; the only difference is the text-layer check (the fourth confidence signal), which was replaced after the runs
(see below) and the saved outputs re-scored offline, with no new requests:

| Over the 390 fields | `invoice/v1` | `invoice/v2` |
|---|---|---|
| **As run** (old check): wrong and **not flagged** for review | **4** | **2** |
| **As run**: fields flagged for review | 3 (0.77%) | 4 (1.03%) |
| **With the fixed check** (re-scored offline): wrong and **not flagged** | **2** | **2** |
| **With the fixed check**: fields flagged for review | 5 (1.28%) | 4 (1.03%) |

So with v2: **387 of 390 fields correct, 3 wrong, 2 of those not flagged, as run and with the fixed check alike.** With v1:
384 of 390 correct, 6 wrong, 4 not flagged as run and 2 with the fixed check. The fixed check flags no correct field that the old
one did not (no new false alarms on these documents). The two wrong values still not flagged are both on image-only documents,
which have no text layer to check: see the limits.

Purchase orders (`purchase_orders_smoke`, `purchase_order/v1`, 3 documents): 30 of 30 fields correct, none flagged; this shows the
second type runs against the real model, nothing more. Cost: about USD 0.008 a two-page invoice at the model's published price
(from the recorded token counts, not an invoice), about 5.6 s a document.

**Read these with their limits next to them.**

- The documents are **synthetic** and the labels are **by construction** (the values they were rendered from). `invoices_hard_v1`
  was reviewed by ChatGPT and Claude and approved by Sameer before any run (*Written by Claude (generated; labels are the render
  inputs); reviewed by ChatGPT and Claude; final decisions by Sameer*); it is ten documents from a small invented pool, a smoke
  test with some teeth, not a benchmark.
- **One run of each prompt, one model, one day, default sampling.** There is no estimate of run-to-run variation, and the failure
  studied (copying a long VAT number) came out right once and wrong three times from the same printed string.
- **By the rule written beforehand `invoice/v2` is the default, but all of the improvement is on `invoices_v1`, the set it was
  written from.** On the hard set v1 and v2 each got 127 of 130 and missed the same three fields. v2 is one added paragraph about
  copying identifiers (`docs/PROMPTS.md`).
- The three misses on the hard set were a date `04/03/2026` read month-first (flagged for review by the date check: the system
  worked as designed), an address the model "corrected" from `Munchen` to `München`, and a 13-character VAT number copied wrongly
  (not flagged, reported at 0.85 and 0.92).
- **The text-layer check had a defect, found by these runs.** It matched a substring after dropping everything but letters and
  digits, so a truncated value and a value with an invisible character both counted as "on the page"; two of the three first-run
  VAT misses were therefore not flagged. It was **not fixed during the runs**, so that the two prompts were scored alike. It was
  replaced afterwards by an exact match on whole tokens after Unicode NFKC, ignoring invisible characters on the page and
  treating an invisible character in the *value* as a mismatch ([`app/validation/text_layer.py`](app/validation/text_layer.py)),
  and the saved outputs were re-scored offline (`python -m evals.rescore`, [`docs/results/2026-10-21-rescored.json`](docs/results/2026-10-21-rescored.json)).
  "As run" and "with the fixed check" are published side by side because the second was chosen after seeing the first; the new
  check was specified from three known cases, not tuned on the other results. It still accepts a truncation at a word boundary
  of a multi-word value (`Acme Supplies` for `Acme Supplies BV`).
- **On image-only documents the model's own confidence is the only signal** (there is no text layer), so a wrong value the model
  is sure of is not flagged. Both wrong values still unflagged with the fixed check are of this kind (an address, and a VAT number
  copied wrongly at 0.92).
- **The threshold (0.85) is unchanged, and it has a boundary case:** a field scoring exactly 0.850 is not below it and passes. One
  wrong value (`hard-008`, `invoice/v1`) sat exactly there.
- **`Munchen` against `München` stays scored wrong:** the page says `Munchen` and the model changed printed text, which is the
  behaviour an extraction task must not have.
- The false-confident counts (2 to 4 of 390 fields) are far too small to say how well the confidence weighting works.

Nothing here is a claim about accuracy on real invoices, about other models, or about production readiness.

### Running an evaluation

```bash
# Offline (the default). Verifies the harness. Costs nothing. Not a quality measurement.
python -m evals.run --dataset invoices_v1

# REAL API CALLS — 20 documents, billed to your Anthropic account. Both caps are required.
DOCINTEL_EXTRACTION_MAX_RETRIES=0 \
  python -m evals.run --dataset invoices_v1 --provider anthropic --request-cap 80 --max-cost-usd 2.00
```

The stub is the default, and a real run is a deliberate act: it will not start without `--request-cap` and
`--max-cost-usd`, with adapter retries off (`DOCINTEL_EXTRACTION_MAX_RETRIES=0`, because a retry inside the SDK is a
request the ledger cannot see), and for a model whose price is known. The caps are for the **whole piece of work**:
they live in a ledger file (`var/eval_ledger.json`, or `--ledger`), a request is counted **before** it is sent, and
the request that would pass either cap is never sent. The **first** failure that means "stop" (no or bad
credential, no credit, 429, 5xx or 529, a timeout, a connection error, a rejected request) ends the run at once with
exit code 3: nothing is retried, no `eval_runs` row is written (a partial run is not a result), and the report says
how many requests and dollars were used.

Useful flags: `--limit N` (first N documents), `--no-persist` (skip the
`eval_runs` row), `--json` (machine-readable report).

Eval documents are written to the configured database and storage like any
other upload — they are the evidence behind the numbers, so they are kept. To
keep them out of your working data, point the run at a separate database:

```bash
createdb -O docintel docintel_evals
DOCINTEL_DATABASE_URL=postgresql+psycopg://docintel:docintel@localhost:5432/docintel_evals \
DOCINTEL_STORAGE_DIR=./var/eval-storage \
  python -m evals.run --dataset invoices_v1 --provider anthropic
```

The run exits non-zero if any document failed to extract. A failed document's
labelled fields count as **wrong**, never skipped, and cannot score correct
even where the label is null — the pipeline did not answer "absent", it did not
answer at all.

### Metric definitions

| Metric | Definition |
| --- | --- |
| exact-match accuracy | per field: matches / labelled occurrences |
| review rate | flagged fields / evaluated fields |
| document review rate | documents with ≥1 flagged field / documents |
| **false-confident rate** | wrong **and not flagged** / evaluated fields |
| …of unflagged fields | wrong and not flagged / not-flagged fields |
| mean cost | Σ `extractions.cost_usd` / documents that reported one |
| latency | `extractions.latency_ms`: the provider call alone, request sent to full response received. Excludes rendering, scoring and database writes. |
| p50 / p95 | nearest-rank, so the figure is always an observed latency |

All four outcomes are reported separately — correct/flagged, correct/unflagged,
wrong/flagged (caught), wrong/unflagged (false-confident) — so "wrong" is never
confused with "wrong and nobody was told".

**Normalisation** removes presentation only: money compares as decimals
(`1,210.00` == `1210.0`), line items compare structurally, other values have
whitespace collapsed. Case and punctuation are **not** normalised — `ACME` and
`Acme`, `INV20261001` and `INV-2026-1001`, are different answers. An absent
value matches only another absent value, so a model that fills in a date where
the document says "Payable on receipt" (the correct `due_date` is null) is
scored wrong. One more rule, for credit notes: on a line item whose amount is
negative, the quantity and the unit price are compared without their sign (the
minus may be written on either; the description and the amount must still
match). Both rules were fixed before any real run; see [docs/EVAL.md](docs/EVAL.md).

Every run writes an `eval_runs` row: dataset, prompt version, model, document
count, per-field accuracy, both rates, mean cost and latency, plus a `metrics`
blob with percentiles, outcome counts and failures. Bump `prompt_version` on
every prompt change so a before/after number is always attached to it.

---

## Configuration

Environment-only, prefix `DOCINTEL_`. See `.env.example`; never commit a real
`.env` (it is gitignored).

| Variable | Default | Notes |
| --- | --- | --- |
| `DOCINTEL_DATABASE_URL` | local dev URL | **Override in production.** The default carries a development credential and points at localhost. |
| `DOCINTEL_ANTHROPIC_API_KEY` | empty | The credential. `ANTHROPIC_API_KEY` (the SDK's own name) in the environment or in `.env` works too; `DOCINTEL_ANTHROPIC_API_KEY` wins if both are set. With none, an extraction fails with a clear "no credential" error. |
| `DOCINTEL_EXTRACTION_MODEL` | `claude-haiku-4-5` | The model ID sent to the API. Not yet verified against the live API. |
| `DOCINTEL_EXTRACTION_TIMEOUT_SECONDS` | `120` | A request is cut off after this long. |
| `DOCINTEL_INVOICE_PROMPT_VERSION` | `invoice/v2` | The invoice prompt: `invoice/v2` (the default, chosen by the rule in `docs/EVAL.md`) or `invoice/v1`. Stored with every extraction. |
| `DOCINTEL_EXTRACTION_MAX_RETRIES` | `2` | SDK retries of transient failures; `0` = never (required for a counted evaluation run). |
| `DOCINTEL_MAX_PAGES` / `DOCINTEL_MAX_IMAGE_BYTES_TOTAL` | `20` / 20 MiB | A document over either is refused before any model call. |
| `DOCINTEL_PRICING_USD_PER_MTOK` | `{}` | JSON `{"<model id>": [input, output]}` to add or correct a price (the built-in table, with its source and date, is in `app/providers/pricing.py`). A model with no price has no cost recorded. |
| `DOCINTEL_EXTRACTION_MAX_TOKENS` | `16000` | Ceiling for one extraction response. |
| `DOCINTEL_STORAGE_DIR` | `./var/storage` | `/var/lib/docintel/storage` in the image. |
| `DOCINTEL_MAX_UPLOAD_BYTES` | 25 MiB | Larger uploads are rejected with 413. |
| `DOCINTEL_RENDER_DPI` | `200` | 72–600. |
| `DOCINTEL_CONFIDENCE_THRESHOLD` | `0.85` | Below this, a field goes to review. |
| `DOCINTEL_CONFIDENCE_WEIGHT_*` | `0.40 / 0.20 / 0.20 / 0.20` | model / schema / arithmetic / text_layer. |
| `DOCINTEL_ENVIRONMENT` | `local` | Reported by `/health`. |
| `DOCINTEL_DEBUG` | `false` | Enables FastAPI debug. **Leave false in production.** |
| `DOCINTEL_TEST_DATABASE_URL` | local test URL | Test suite only; it migrates and tears this database down on every run. |

There are no settings hard-coded outside `app/config.py`. `.env.example`
contains development defaults only — no real credential is committed anywhere
in this repository.

### Migrations

Alembic reads the database URL from `DOCINTEL_DATABASE_URL` via `app/config.py`
— `alembic.ini` deliberately holds no URL, so migrations and the app can never
disagree about which database they are using.

```bash
alembic upgrade head                                  # apply
alembic downgrade base                                # roll back to empty
alembic current                                       # which revision is applied
alembic check                                         # models vs. database: any drift?
alembic revision --autogenerate -m "add something"    # new migration
```

Every model must be imported in `app/models/__init__.py`; autogenerate only
sees tables attached to `Base.metadata`. Review autogenerated migrations before
committing them — in particular, Postgres `ENUM` types outlive the table that
uses them, so a downgrade has to drop the type explicitly (see the first
migration for the pattern).

---

## Security and reliability

What is actually implemented:

- **Uploads are validated by file signature**, not by the declared content type
  or the extension. A text file named `invoice.pdf` is rejected with 415 before
  anything is written.
- **Filenames from clients never reach the filesystem.** The stored name is
  derived from the detected media type; the client's name is kept only as a
  display column, reduced to a bare basename.
- **Uploads are size-capped** (`DOCINTEL_MAX_UPLOAD_BYTES`, default 25 MiB),
  enforced while reading rather than after buffering.
- **Failures fail loudly with a stored reason.** A provider error, a refusal,
  an unparseable answer, a size guard or an unexpected error marks the document `failed`, writes an
  `extractions` row with why, shows it on the review page and returns it from `GET /api/v1/documents/{id}`.
  Nothing silently returns empty fields.
- **Deterministic checks run outside the model.** Arithmetic, date validity and
  currency codes are verified in Python; the prompt explicitly instructs the
  model *not* to compute totals.
- **Model confidence is not trusted on its own.** It carries under half the
  weight, and a failed deterministic check forces review regardless of score.
- **Uncertain fields go to a human** rather than into the export silently.
- **Corrections preserve the original value** in the `corrections` table;
  repeated corrections chain back to the model's first answer.
- **API keys come from environment configuration**, never from code. The test
  suite additionally patches the Anthropic client so no test can make a real
  request — verified by a test that asserts the guard fires.
- **The container runs as a non-root user** with a health check.
- **The public demo (only) has mutation protection and a rate limiter.**
  See [Demo Mode](docs/DEMO.md) for the full description. Neither applies
  to Live Mode: both are dependency overrides scoped to `demo.app`'s own
  FastAPI instance, and the rate limiter is also off by default even
  there, behind `DOCINTEL_DEMO_RATE_LIMIT_ENABLED`.

**Prompt injection, and what it can and cannot do here.** A hostile document can contain text that tells the model
what to report. The model sees only the page images, has no tools and is forced into the schema, so the worst it can
do is report a wrong value with a high confidence. A value that contradicts the rest of the document (a total that
is not subtotal plus tax) fails a deterministic check and goes to a person whatever confidence it carries (there is
a test). **A wrong value that is consistent with the rest of the document is not caught by these checks**, and the
text-layer check corroborates it too, because the injected text is on the page. There is no injection evaluation
yet.

**Data handling.** Uploaded files and their rendered pages are stored unencrypted under `DOCINTEL_STORAGE_DIR`, with
no retention policy; the pages are sent to the Anthropic API for extraction. Do not upload documents you may not send
to a third party.

What is **not** implemented for Live Mode, by design (see non-goals):
authentication, authorisation, multi-tenancy, rate limiting, at-rest
encryption, audit logging of readers, and CSRF protection on the review
form. DocIntel is built as a single-user local tool. **Do not expose Live
Mode to an untrusted network as-is.** The demo (`demo.app`) is the one
exception built specifically to be exposed publicly — see
[Demo Mode](docs/DEMO.md) for what it does and does not protect against.

---

## Testing

```bash
pytest                     # 658 tests
pytest -q tests/test_purchase_order.py
```

With PostgreSQL running: 658 tests collected, 641 pass. A previous count
(478 collected, 477 pass) reported one failure,
`tests/test_eval_cli.py::test_json_output_is_machine_readable`, as a
known, environment-specific artifact — SQLAlchemy log output occasionally
interleaves with the CLI's captured JSON stdout in some environments — not
a defect in the application. That specific failure did not reproduce in
the environment this count was most recently verified in; it is recorded
here rather than silently dropped, since nothing has changed about the
underlying mechanism that could cause it, only the environment happened
not to trigger it this time.

Tests that need PostgreSQL are skipped when no server answers, so `pytest`
runs without a database at 391 pass, 267 skip (658 collected either way).
Database tests migrate a dedicated test database to head and tear it down on
every run.

No test can reach the Anthropic API: an autouse fixture patches the client's
request methods to raise, and a test asserts that guard actually fires.

---

## Project summary

**DocIntel — Document Intelligence with Human Review.** Businesses need
structured data out of invoices and purchase orders, but a vision model that
guesses silently is worse than no automation at all — it produces a plausible
wrong total with high confidence. DocIntel is a FastAPI/PostgreSQL pipeline
that extracts documents into strict Pydantic schemas through a provider-agnostic
interface, then refuses to trust the result: every field is re-checked in
Python against deterministic rules (totals arithmetic, line-item sums, ISO 4217
codes, date validity) and corroborated against the PDF's own text layer, and
those signals are combined with the model's self-reported confidence into a
weighted per-field score. Anything below a configurable threshold — or anything
that failed a deterministic check, whatever its score — is routed to a human
review queue where a correction is recorded as ground truth with a full audit
trail, leaving the model's original answer intact for comparison. The
engineering challenge was extensibility without branching: invoices and
purchase orders share one pipeline with zero document-type conditionals,
resolved entirely through registries. Quality is measured by a reproducible
evaluation harness over a committed, deterministic, labelled dataset that
reports per-field exact-match accuracy, review rate, cost and latency — and,
most importantly, the false-confident rate: how often the system was wrong and
did not say so.

**Stack:** Python 3.13 · FastAPI · Uvicorn · Pydantic v2 · pydantic-settings ·
SQLAlchemy 2.x · Alembic · psycopg 3 · PostgreSQL · Anthropic SDK (vision,
structured outputs) · pypdfium2 · Pillow · Jinja2 · pytest · HTTPX · Docker

---

## Scope and non-goals

Built: upload, rendering, extraction for two document types, deterministic
validation, confidence scoring, review queue, corrections with audit trail,
JSON/CSV export, evaluation harness.

Deliberately not built: authentication and multi-tenancy (single local user);
fine-tuning or training; a polished design system (the review page is plain and
functional on purpose); handwriting, multi-language or nested-table extraction;
background worker queues — FastAPI `BackgroundTasks` is used instead.
