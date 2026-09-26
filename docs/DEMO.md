# Demo Mode

Demo Mode lets anyone run DocIntel's real extraction, validation, scoring,
review and export pipeline **without an Anthropic API key** and **without
any network call**, using three pre-seeded sample invoices. It is the one
part of DocIntel built and hardened to be put on the public internet — see
§13 for exactly what that means and does not mean.

It exists to make the pipeline runnable and demonstrable in five minutes,
for someone who does not have (and should not need) production credentials
just to see how the system behaves.

## 1. What Demo Mode is

Demo Mode is the real DocIntel FastAPI application
(`app.main.create_app()`) with two things overridden: the extraction
provider, and the mutation guard (§13). Instead of calling Anthropic's
vision API, it hands the pipeline a `DemoExtractionProvider`
(`demo/providers.py`) that returns a committed, hand-verified answer for
a small set of known sample invoices; instead of allowing every write,
it refuses uploads and corrections with 403.

Nothing about the pipeline itself changes. Page rendering, extraction
triggering, schema parsing, deterministic validation, confidence scoring,
persistence, the review queue, the audit trail, and export are all the
exact same production code paths used with a real Anthropic key. There is
no second pipeline and no demo-only route. What *is* different from Live
Mode, deliberately, is which requests are allowed to reach that pipeline
at all — see §13.

## 2. Deterministic and credential-free

`DemoExtractionProvider` never imports `anthropic`, never makes an HTTP
request, and never loads a model. Given the same input document, it always
returns the same answer, byte for byte. No `DOCINTEL_ANTHROPIC_API_KEY` is
read anywhere on this path.

## 3. Uses committed synthetic invoices

The recognised documents are three of the synthetic sample invoices already
committed under `evals/datasets/invoices_v1/` for the evaluation harness —
`invoice-001.pdf`, `invoice-003.pdf`, `invoice-004.pdf`. These are not real
customer documents; they were generated for testing and evaluation and are
already tracked in the repository.

## 4. How fixture matching works

`ExtractionProvider.extract()` only ever receives the document's **rendered
page images** (`PageImage` objects — one PNG per page, produced by
`app/rendering.py`), never the originally-uploaded file's own bytes. So
`DemoExtractionProvider` computes the SHA-256 of the concatenated rendered
page PNGs (in page order) and looks that hash up in a small committed table,
`demo/fixtures/invoice_answers.json`.

- **Known hash** → the committed fixture answer is returned, run through
  the real schema, validation, scoring, and persistence pipeline.
- **Unknown hash** → a `ProviderError` is raised, exactly like any other
  extraction failure. There is no approximate matching, no silent empty
  answer, and no fallback to a real model.

Because matching is by content, re-uploading the same sample PDF — even
under a different filename — is recognised. A different document, even a
visually similar one, is not.

## 5. Which invoices are supported

| Fixture | Source PDF | Notes |
| --- | --- | --- |
| `invoice-001` | `evals/datasets/invoices_v1/invoice-001.pdf` | Straightforward two-line-item invoice; all fields high confidence. |
| `invoice-003` | `evals/datasets/invoices_v1/invoice-003.pdf` | `purchase_order_number` is genuinely absent on this invoice; its fixture confidence is deliberately authored below the default review threshold (see §10) so the review queue is exercised honestly. |
| `invoice-004` | `evals/datasets/invoices_v1/invoice-004.pdf` | A third invoice, for variety (different currency/line-item shape). |

Any other document — including the other invoices in the same dataset
directory, and any purchase order — is **not** recognised by Demo Mode and
will fail extraction with a clear error (§9, §12).

## 6. How to start the demo locally

Demo Mode still needs a real, migrated PostgreSQL database — only the
*extraction provider* is stubbed, not storage or the database. Because
uploads are refused (§13), the three known sample invoices must be
**seeded** before the app is useful.

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # DOCINTEL_ANTHROPIC_API_KEY can stay unset

createuser docintel --pwprompt
createdb -O docintel docintel

alembic upgrade head
python -m demo.seed           # prints each fixture's document id
uvicorn demo.app:app --reload
curl localhost:8000/api/v1/health
```

`python -m demo.seed` is idempotent (`demo/seed.py`): re-running it after
the documents already exist changes nothing. It prints the three document
ids it seeded (or already found) — the same three fixed ids listed in §7,
so you rarely need to run it just to look them up.

The only other difference from the normal "Running it" instructions in
the main [README](../README.md#running-it) is the module passed to
`uvicorn`: `demo.app:app` instead of `app.main:app`.

### Docker

`docker-compose.yml` has a dedicated `demo` service — the same image as
`app`, built from the same `Dockerfile`, with its command overridden to
serve `demo.app:app` instead — and a dedicated `seed` service that runs
`python -m demo.seed` once, after migrations, before `demo` starts. Both
depend on `db`/`migrate` the way `app` does, so nothing comes up before
the database is healthy and migrations have run; no second migration
mechanism, no Anthropic key. `app` (Live Mode) does not depend on `seed`
and is unaffected by it.

```bash
docker compose up --build db migrate seed demo
curl localhost:8001/health
docker compose logs seed      # each seeded document's id
```

The demo is published on host port `8001` (`app`, when also running, keeps
`8000`), so both can run side by side without a port conflict.

## 7. The complete demo workflow

Uploading is refused in Demo Mode (§13), so the workflow starts from an
already-seeded document rather than an upload step. Each of the three
fixtures has a **fixed, deterministic document id** (`demo/seed.py::
seed_document_id`, a `uuid5` of the fixture's own name) — the same one on
every fresh seed, so these ids are safe to hardcode here rather than
looked up:

| Fixture | Document id |
| --- | --- |
| `invoice-001` | `05f7f178-1d0a-594f-8ada-1898ff4e7706` |
| `invoice-003` | `1d1d8194-747f-58a2-b9c2-9397bca02ea8` |
| `invoice-004` | `07b03de9-7cc1-5e79-9379-0d450f3e6215` |

```bash
# 1. Trigger extraction on the already-seeded invoice-001 — served by
#    DemoExtractionProvider, not Anthropic. Rate-limited when
#    DOCINTEL_DEMO_RATE_LIMIT_ENABLED is set (§13) — 429 + Retry-After
#    past the configured per-minute count.
curl -X POST localhost:8000/api/v1/documents/05f7f178-1d0a-594f-8ada-1898ff4e7706/extract
# → 202

# 2. Inspect the extracted fields
curl 'localhost:8000/api/v1/documents/05f7f178-1d0a-594f-8ada-1898ff4e7706/export?format=json' | jq .fields

# 3. Open the review queue (invoice-003 will have a field here — see §10)
curl -X POST localhost:8000/api/v1/documents/1d1d8194-747f-58a2-b9c2-9397bca02ea8/extract
curl localhost:8000/api/v1/review | jq '.items[] | {field_name, confidence, review_reason}'
```

Submitting a correction (`POST /api/v1/review/{field_id}` or the `/review`
page's form) is part of the **local, non-public** walkthrough only — in
the public demo it refuses with 403 (§13). Locally, against your own
non-public `uvicorn demo.app:app` (no rate limiter, guard still active by
virtue of running `demo.app`), you can still see extraction, scoring, and
the review queue populate exactly as they would with a real key; only the
write step is unavailable, the same as it is in the hosted public demo.

The review page at `/review` in a browser reflects the same read-only
behaviour — it is the same server-rendered page the production app
serves, its form simply returns 403 on submit here.

## 8. What the demo demonstrates

- The full pipeline wired together end to end, from a seeded document
  through render → extract → validate → score → persist → review → export.
  Correction and the audit trail run through the identical code, provably
  so (`tests/test_demo_guard.py`), but are only reachable locally, not
  through the public-facing guard (§13).
- That deterministic validation (date/currency/arithmetic checks) and
  confidence scoring run for real against a real extraction result, not a
  canned "review-ready" record.
- That a field scored below the confidence threshold genuinely routes the
  document to `NEEDS_REVIEW` and appears in the review queue.
- That the application architecture cleanly separates "where extracted
  data comes from" from everything downstream of it — the same seam a test
  double uses is the seam Demo Mode uses.

## 9. What is NOT being demonstrated

- **Real model accuracy.** Demo Mode says nothing about how well Anthropic's
  vision model extracts real invoices. See [Evaluation](../README.md#evaluation)
  in the main README for the real accuracy methodology and its current status.
- **Extraction on arbitrary documents.** Demo Mode recognises exactly three
  committed sample PDFs. Anything else fails closed (§12).
- **Real latency, cost, or token usage.** These are `None` for every demo
  extraction (§10), not simulated.
- **OCR or vision-model robustness** to scan quality, handwriting, unusual
  layouts, etc. — the fixture bypasses the model entirely.

## 10. Fixture confidence is hand-authored demo data, not model confidence

Every `confidence` value inside `demo/fixtures/invoice_answers.json` was
written by hand for this milestone. It is **not** a real Anthropic model
confidence score, and it is not derived from any model run.

Two of the three fixtures use uniformly high hand-authored confidence,
reflecting that those fields are unambiguous on the source document.
`invoice-003`'s `purchase_order_number` is a deliberate exception: that
field is genuinely absent on the invoice, and its fixture confidence
(`0.55`) is intentionally authored below the default review threshold
(`confidence_threshold = 0.85`) specifically so that uploading it exercises
the real review queue through the real scoring logic — not to fabricate a
review state. This choice is documented inline in the fixture file's
`notes` field and in `demo/providers.py`.

Everything downstream of this raw, fixture-supplied `model_confidence` —
the four-signal weighted scoring, the blocking-signal override, and the
final `needs_review` decision — is the same unmodified `app/confidence.py`
logic used for a real Anthropic extraction.

`RawExtraction.input_tokens`, `.output_tokens`, `.cost_usd`, and
`.latency_ms` are all `None` for every demo extraction — no token count,
cost, or latency is fabricated. `RawExtraction.metadata` and
`raw_response` both carry `"demo_fixture": True` plus the fixture id, so a
demo-sourced `Extraction` row is always identifiable as such.

## 11. Real Anthropic extraction still requires an API key

Demo Mode does not change or remove the requirement for a real
`DOCINTEL_ANTHROPIC_API_KEY` when running the normal application
(`app.main:app`) against a document that is not one of the three known
fixtures. `get_provider()`, `AnthropicExtractionProvider`, and the normal
`/api/v1/documents/{id}/extract` behavior are completely unmodified by
Demo Mode — see the main [README](../README.md) for that configuration.

## 12. Unknown or non-fixture documents intentionally fail closed

Uploading and triggering extraction on any document Demo Mode does not
recognise — a different PDF, a scan of one of the three known invoices, an
image, or a purchase order — raises a `ProviderError` inside the real
extraction pipeline, which the application already handles the same way it
handles any other extraction failure: the document is marked `FAILED` with
the error recorded, nothing is silently guessed, and no request is ever
sent to a real model.

This is intentional. Demo Mode's purpose is to show the real pipeline
behaving correctly on known input, not to approximate a general-purpose
extractor.

## 13. Public-demo hardening: mutation protection and rate limiting

Everything above describes Demo Mode's data-and-pipeline behaviour, which
predates and is unaffected by this section. This section describes the
two things added specifically so `demo.app` — and only `demo.app`, never
`app.main` (Live Mode) — is safe to put on the public internet as a
portfolio demonstration.

### Mutation protection

`app/api/demo_guard.py::require_mutation_allowed` is a no-op FastAPI
dependency by default — Live Mode never blocks anything through it.
`demo/app.py` overrides it, on its own FastAPI instance only
(`app.dependency_overrides`, the same seam the extraction provider is
already swapped through), to unconditionally refuse with 403:

- `POST /api/v1/documents` (upload)
- `POST /api/v1/review/{field_id}` (JSON correction)
- `POST /review/{field_id}` (the review page's form target)

`POST /api/v1/documents/{id}/extract` and every `GET` route (health,
export, the review queue, the review page) are **not** gated by this
dependency — they are the demo's intended, safe, read/trigger surface.
Extraction is safe to leave open by construction, not because it is
trusted: `demo.app`'s own provider override means it is structurally
incapable of ever reaching a real Anthropic call, regardless of which
document id is passed to it.

Because upload is refused, the demo cannot generate its own content — see
§6/§7 for why seeding through `demo/seed.py` exists and is required.
`tests/test_demo_guard.py` proves the guard on a real, seeded document
(not an invented id a 404 could mask a still-broken guard behind) and
that no `Correction` row is ever written when a demo request is refused.

### Rate limiting

`app/api/rate_limit.py::rate_limit_demo_extraction` is a second,
independent dependency, applied only to `POST
/api/v1/documents/{id}/extract` — the one route that does real,
non-trivial work per call. It is:

- **Off by default** (`Settings.demo_rate_limit_enabled = False`) —
  Live Mode and any deployment that never sets
  `DOCINTEL_DEMO_RATE_LIMIT_ENABLED` is completely unaffected.
- **In-process and in-memory** — a sliding window of request timestamps
  per client, held in a single Python object for the life of the
  process. No Redis, no database table, no external service.
- **Keyed by `Request.client.host`** — the direct ASGI peer address,
  never `X-Forwarded-For` or any other client-supplied header, which
  would let a visitor set their own value and evade the limit entirely.
  No reverse proxy or hosting platform has been chosen yet for a public
  deployment; if one is added later, whatever it puts in front of this
  process will need its own, deliberately-trusted mechanism for the
  real client IP — an explicit follow-up, not assumed here.
- **10 requests per minute per client, by default**
  (`Settings.demo_rate_limit_per_minute`), configurable via
  `DOCINTEL_DEMO_RATE_LIMIT_PER_MINUTE` — conservative on purpose: generous
  enough for a visitor trying the three seeded invoices a few times over,
  tight enough to stop a scripted loop quickly.
- **`docker-compose.yml`'s `demo` service sets
  `DOCINTEL_DEMO_RATE_LIMIT_ENABLED=true`** — `app` (Live Mode) does not,
  and is unaffected.

An excess request gets `429` with a `Retry-After` header naming how many
seconds until the oldest request in the current window ages out.

### Single-replica limitation

The in-memory design is a deliberate, disclosed trade-off, not an
oversight: it is correct and sufficient for exactly the deployment this
demo is built for — **one container, one replica**, which is what
`docker-compose.yml`'s `demo` service is. If this demo is ever scaled to
more than one replica, each would keep its own independent count, so the
effective limit becomes `per_minute × replica_count` rather than a true
global limit. Fixing that would mean a shared store (e.g. Redis) — not
introduced here because nothing about this demo's intended deployment
needs it yet.

### What is still not claimed

None of the above is a general-purpose security model. Public exposure of
`demo.app` is deliberately narrow: three fixed, publicly-known documents,
no user data, no credential, no write surface beyond what §13 already
describes as blocked. Everything the main README's **Security and
reliability** section says is *not* implemented for Live Mode (auth,
multi-tenancy, at-rest encryption, CSRF protection, audit logging of
readers) remains not implemented here either — the demo is safe to expose
publicly because its write surface and its expensive route are both
addressed, not because those other properties were added.
