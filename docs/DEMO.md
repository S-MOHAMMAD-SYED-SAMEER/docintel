# Demo Mode

Demo Mode lets anyone run DocIntel's real extraction, validation, scoring,
review, correction and export pipeline **without an Anthropic API key** and
**without any network call**, using a small set of committed sample invoices.

It exists to make the pipeline runnable and demonstrable in five minutes,
for someone who does not have (and should not need) production credentials
just to see how the system behaves.

## 1. What Demo Mode is

Demo Mode is the real DocIntel FastAPI application
(`app.main.create_app()`) with exactly one thing swapped: the extraction
provider. Instead of calling Anthropic's vision API, it hands the pipeline
a `DemoExtractionProvider` (`demo/providers.py`) that returns a committed,
hand-verified answer for a small set of known sample invoices.

Nothing else changes. Upload, page rendering, extraction triggering,
schema parsing, deterministic validation, confidence scoring, persistence,
the review queue, corrections, the audit trail, and export are all the
exact same production code paths used with a real Anthropic key. There is
no second pipeline, no demo-only route, and no demo-only review UI.

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
*extraction provider* is stubbed, not storage or the database.

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # DOCINTEL_ANTHROPIC_API_KEY can stay unset

createuser docintel --pwprompt
createdb -O docintel docintel

alembic upgrade head
uvicorn demo.app:app --reload
curl localhost:8000/api/v1/health
```

The only difference from the normal "Running it" instructions in the main
[README](../README.md#running-it) is the module passed to `uvicorn`:
`demo.app:app` instead of `app.main:app`.

### Docker

`docker-compose.yml` has a dedicated `demo` service — the same image as
`app`, built from the same `Dockerfile`, with its command overridden to
serve `demo.app:app` instead. It depends on `db` and `migrate` exactly the
way `app` does, so it comes up only once the database is healthy and
migrations have run; no second migration mechanism, no Anthropic key.

```bash
docker compose up --build db migrate demo
curl localhost:8001/health
```

The demo is published on host port `8001` (`app`, when also running, keeps
`8000`), so both can run side by side without a port conflict.

## 7. The complete demo workflow

Same nine steps as the main README's demo walkthrough, using one of the
three known sample invoices:

```bash
# 1. Upload a known sample invoice
curl -F file=@evals/datasets/invoices_v1/invoice-001.pdf \
     -F doc_type=invoice \
     localhost:8000/api/v1/documents
# → 201, { "id": "...", "status": "uploaded" }

# 2. Trigger extraction — served by DemoExtractionProvider, not Anthropic
curl -X POST localhost:8000/api/v1/documents/<document_id>/extract
# → 202

# 3. Inspect the extracted fields
curl 'localhost:8000/api/v1/documents/<document_id>/export?format=json' | jq .fields

# 4. Open the review queue (invoice-003 will have a field here — see §10)
curl localhost:8000/api/v1/review | jq '.items[] | {field_name, confidence, review_reason}'

# 5. Correct a field, same as with a real key
curl -X POST localhost:8000/api/v1/review/<field_id> \
     -H 'content-type: application/json' \
     -d '{"corrected_value": "1210.00"}'

# 6. Export the corrected record
curl 'localhost:8000/api/v1/documents/<document_id>/export?format=json'
```

The review page at `/review` in a browser works identically — it is the
same server-rendered page the production app serves.

## 8. What the demo demonstrates

- The full pipeline wired together end to end: upload → render → extract →
  validate → score → persist → review → correct → export.
- That deterministic validation (date/currency/arithmetic checks) and
  confidence scoring run for real against a real extraction result, not a
  canned "review-ready" record.
- That a field scored below the confidence threshold genuinely routes the
  document to `NEEDS_REVIEW` and appears in the review queue.
- That a correction is persisted with the original value retained in the
  audit trail, and that export reflects the corrected record.
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
