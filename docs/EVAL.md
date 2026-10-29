# Evaluation: the plan for the first real-model runs, written before any of them

**Status: the planned real-model runs of 2026-10-21 are done except the recording (see "Record of the runs" at the end and `docs/results/2026-10-21-real-runs.md`).** This file was committed before any request, so the plan and the decision rule could not
be bent to a result. When the runs have been made, their numbers go in `docs/results/` and are summarised in the README
with their limits; this file keeps the plan as it was written.

What is measured and how is in the README ("Evaluation", "Metric definitions"). This file says **what will be run, with
what caps, in what order, and how the one prompt revision will be decided.**

## Model and settings

- Model: `claude-haiku-4-5` (the default in `app/config.py`). Its price (USD 1 per million input tokens, 5 per million output
  tokens) was checked against Anthropic's pricing page on 2026-10-20 (`app/providers/pricing.py`). The model ID itself was
  **not yet verified against the live API** when this plan was written; the 2026-10-21 runs verified it (the API resolved it to `claude-haiku-4-5-20251001`).
- Adapter retries **off** (`DOCINTEL_EXTRACTION_MAX_RETRIES=0`), timeout 120 s, `max_tokens` 16,000, the default prompts
  `invoice/v1` and `purchase_order/v1`, the default confidence weights (0.40 / 0.20 / 0.20 / 0.20) and the default threshold
  (0.85). **None of the weights or the threshold is tuned on the evaluation data.** If an offline analysis shows what other
  values would have done, that is reported as in-sample and not applied.
- The key is only in the git-ignored `docintel/.env`, which is never printed or read by anyone but the process; the runs
  remove any inherited `ANTHROPIC_API_KEY` from the environment first.

## Caps, for the whole piece of work

**80 model requests and USD 2.00 in total**, counted in a ledger file before each request is sent (see `evals/budget.py`
and the README). A run stops at the first of: the request cap, the cost cap, or any failure that means stop (no or bad
credential, no credit, 429, 500/503/529, a timeout, a connection error, a rejected request). Nothing is retried; a
stopped run writes no `eval_runs` row and is **reported, not re-run quietly**. The ledger count and the dollars are
logged at the end of every run.

## The datasets

| Dataset | Documents | What it is | Status |
|---|---|---|---|
| `invoices_v1` | 20 | Synthetic invoices, ten-point Courier PDFs with a clean text layer, labels are the values they were rendered from | committed |
| `invoices_hard_v1` | 10 | Synthetic invoices that are harder: five images with no text layer (a scan, a photograph, a stamp and a scribble), skew, blur, other layouts, European formats, a credit note, a missing due date, a two-page table | **approved by Sameer on 2026-10-21 with no label changes; frozen (sha256 below) before any real run** |
| `purchase_orders_smoke` | 3 | Proves the second document type runs end to end; not a benchmark | committed |

Provenance, stated wherever a number from the hard set is quoted: *Written by Claude (generated; labels are the render
inputs); reviewed by ChatGPT and Claude; final decisions by Sameer.* ChatGPT reviewed every field of every document and found
0 wrong labels and 0 unreadable documents; Sameer approved with no label changes. The hard set's labels are never changed
after a model has seen the documents; a change would be a new dataset version.

### The hard set is frozen

`python -m evals.freeze invoices_hard_v1` prints one sha256 over every file of the dataset (text files with line endings
normalised; images and PDFs as they are), and `tests/test_hard_set_frozen.py` fails if any file differs from it.

```
hard-set-sha256: db2962fdacc1b5c84577bb062076b47875103c73cd9b771b5409c3206149cc24
```

### Two scoring rules, decided with the approval and written before any run

1. **Credit note (`hard-004`).** A line item whose amount is negative counts as correct if the amount is the correct negative
   value, whether the minus is written on the quantity or on the unit price. In `evals/metrics.py`, on a row with a negative
   amount the quantity and the unit price are compared without their sign; the description and the amount must still match,
   so a lost sign on the amount, a different quantity or a different price is still wrong. A row with a positive amount is
   compared exactly as before.
2. **"Payable on receipt" (`hard-009`).** The correct `due_date` is null. A model that fills in a date (the invoice date, for
   example) is scored wrong; an absent value matches only another absent value (this was already the rule, and is now tested
   for this case).

Both are tested (`tests/test_eval_metrics.py`) and apply to every run, including the runs of `invoices_v1`, where no row has a
negative amount and no due date is null in a way that changes anything.

## The order, and the request budget

| Step | Requests | Notes |
|---|---|---|
| 0. Pre-flight (no request) | 0 | the key's variable **name** is present in `.env`; the model name is spelled right; the database and storage are named explicitly; the ledger is fresh; the dataset hash matches |
| 1. One smoke extraction through the live app, one invoice | 1 | confirms the schema is accepted and the model ID is valid |
| 2. `invoices_v1` with prompt `invoice/v1` | 20 | |
| 3. `invoices_hard_v1` with `invoice/v1` | 10 | **its per-document results are not read until step 5 is committed** (see below) |
| 4. Offline analysis of step 2's stored raw replies | 0 | where the model was wrong and not flagged; the reasons |
| 5. At most one prompt revision, `invoice/v2`, written from step 4 **only**, with its note, **committed before any further run** | 0 | |
| 6. `invoices_v1` and `invoices_hard_v1` with `invoice/v2` | 30 | the hard set is the out-of-sample check: `invoice/v2` was not written from its failures |
| 7. `purchase_orders_smoke` with `purchase_order/v1` | 3 | |
| 8. Recording of the video on the real app | up to 12 | at most 3 takes, about 3 to 4 requests a take |
| **Total** | **about 76 of 80** | USD about 1 at the model's price (a document is roughly 5,000 to 6,000 input and under 1,000 output tokens); the USD 2.00 cap is the binding safety |

If a step is interrupted, it is reported and not repeated unless Sameer says so.

## The decision rule for the prompt revision (written now)

`invoice/v2` becomes the default prompt only if, **across the 30 documents of steps 2/3 against steps 6** (the same
documents, the same settings, the same thresholds), **all** of these hold:

1. **False-confident fields**: v2 has no more wrong-and-not-flagged fields than v1.
2. **The three acceptance fields** (`invoice_number`, `invoice_date`, `total`): v2's exact-match count over the 30 documents is
   no lower than v1's.
3. **Overall exact-match accuracy** over all evaluated fields is no lower than v1's.
4. **Review rate** is no more than 5 percentage points higher than v1's (a revision that "improves" by flagging everything does not count).
5. **And** at least one of: fewer wrong fields in total, or fewer false-confident fields.

Otherwise `invoice/v1` stays the default. Both runs of both prompts are published whatever they show. There is no third
prompt and no re-run to break a tie. With 30 documents and a single run each, a difference of one or two fields is within
what another run could change; the report says how large each gap is, and the README says that the improvement, if there is
one, is **in-sample for `invoices_v1`** (the revision is written from its failures) and **out-of-sample only for the hard set**.

## What this will not show (stated now, so it is not discovered later)

- Accuracy on real invoices: every document is synthetic and from a small invented pool of vendors and customers.
- Anything about handwriting, other languages beyond the two European formats here, tables nested in tables, or documents
  longer than two pages.
- That the confidence weights and the threshold are right: they are untuned defaults, and the number of wrong fields is small.
- Anything about other models, or about repeatability: one run of each, at the model's default sampling.
- Anything about cost beyond the arithmetic from the recorded token counts and the price on 2026-10-20.

## Where results go

`docs/results/<date>-<dataset>-<model>-<prompt>.json` (the report as `evals.run --json` prints it, plus the ledger totals) and a
short `.md` beside each; the README's "Evaluation" section is rewritten from those files only. A test (as in the other
projects) checks that every figure the README prints is in the committed result files.

## Record of the runs (added as they happen; the plan above is unchanged)

| Date | Step | What | Requests / USD (ledger) |
|---|---|---|---|
| 2026-10-21 | 1 | One smoke extraction of `invoice-001` through the live app. Not part of any result. | 1 / $0.0082 |
| 2026-10-21 | 2 | `invoices_v1`, `invoice/v1`, 20 documents: completed, no document failed to extract | 21 / $0.174 |
| 2026-10-21 | 3 | `invoices_hard_v1`, `invoice/v1`, 10 documents: completed. **Not read** (only the ledger line was looked at) | 31 / $0.243 |
| 2026-10-21 | 4 | Offline analysis of step 2's stored replies: three `vendor_tax_id` misses on one repeated string; see `docs/PROMPTS.md` | 0 |
| 2026-10-21 | 5 | `invoice/v2` written from step 4 only and committed (this file's decision rule applies unchanged) | 0 |

| 2026-10-21 | 6 | `invoices_v1` with `invoice/v2` (20) and `invoices_hard_v1` with `invoice/v2` (10): completed | 61 / $0.482 |
| 2026-10-21 | 7 | `purchase_orders_smoke`, `purchase_order/v1`, 3 documents: completed | 64 / $0.505 |
| 2026-10-21 | 8 | The recording: **not yet made** | |

**Ledger after the runs: 64 of 80 requests and $0.505365 of $2.00.** No 401, 403, 429, 503, 529, quota or credit error; nothing was retried.

### What the runs found, and the decision

| Over the same 30 documents (390 fields) | v1 | v2 | The rule | Met |
|---|---|---|---|---|
| Wrong fields | 6 of 390 | 3 of 390 | (5) fewer wrong fields or fewer false-confident | yes |
| False-confident fields | 4 | 2 | (1) no more than v1 | yes |
| Acceptance fields correct (`invoice_number`, `invoice_date`, `total`) | 89 of 90 | 89 of 90 | (2) no lower | yes |
| Overall exact-match | 384 of 390 | 387 of 390 | (3) no lower | yes |
| Fields flagged for review | 3 of 390 (0.77%) | 4 of 390 (1.03%) | (4) at most 5 points higher | yes |

**`invoice/v2` becomes the default** (`DOCINTEL_INVOICE_PROMPT_VERSION`, default `invoice/v2`), by the rule as written. Read it with these limits:
all of the difference is on `invoices_v1` (257 of 260 correct with v1, 260 of 260 with v2), the set v2 was written from; on the hard set
(the out-of-sample check) v1 and v2 each got 127 of 130 and missed the same three fields. Each prompt was run once on each set at the
model's default sampling, on synthetic documents whose labels are by construction, with one model. The runs are not an accuracy figure and
support no claim about real invoices. Per-field results, every wrong field and the surprises are in `docs/results/2026-10-21-real-runs.md`.

### The text-layer check, fixed after the runs, and the saved outputs re-scored (2026-10-21)

**Why.** The runs found that the check "is the extracted string on the page?" (the fourth confidence signal) accepted a
truncated value and a value with an invisible character. It was left alone during the runs so that v1 and v2 were scored alike.

**What changed** (`app/validation/text_layer.py`, decided by Sameer): exact match on whole tokens after Unicode NFKC and
case-folding; the page is split on whitespace and only punctuation, symbols and separators *inside* a token are ignored
(`1210.00` still matches `EUR 1,210.00`); there is no substring pass; invisible (Unicode "format") characters are dropped from the
page, because text extraction can insert them, but a value that contains one is never found. Tests include the three real cases
(a dropped last digit, a dropped middle digit and a zero-width space), in `tests/test_text_layer.py`.

**Re-scoring.** `python -m evals.rescore` re-scores the saved outputs offline, with no request: model replies, extracted values and
labels are read from `docs/results/` and not changed; only the text-layer signal is recomputed, from the dataset's document files.
It first recomputes the check that was in use and requires every saved flag and confidence to be reproduced exactly (they were:
all 810 fields of the five runs; `tests/test_rescore.py` checks it), then recomputes with the new one. Output: `docs/results/2026-10-21-rescored.json`.

| Over the same 30 documents (390 fields) | `invoice/v1` as run | `invoice/v1` fixed check | `invoice/v2` as run | `invoice/v2` fixed check |
|---|---|---|---|---|
| Wrong fields | 6 | 6 | 3 | 3 |
| Wrong and not flagged (false-confident) | 4 | 2 | 2 | 2 |
| Wrong and flagged | 2 | 4 | 1 | 1 |
| Correct but flagged | 1 | 1 | 3 | 3 |
| Flagged in all | 3 (0.77%) | 5 (1.28%) | 4 (1.03%) | 4 (1.03%) |

The decision rule above was evaluated on the **as run** numbers, as written, and its outcome (v2 is the default) is not changed
by the fix: on the fixed numbers v2 is still no worse on any of its five conditions. The fixed check turned no correct field into a
flagged one on these documents.

**Known limits, stated here so they are not discovered later:**
- The two wrong values still unflagged (one address and one VAT number, both on image-only documents) cannot be caught by this
  signal: **on an image-only document the model's own confidence is the only signal.**
- **The threshold stays at 0.85.** A final score of exactly 0.850 is not below it, so it passes: one wrong VAT number (`hard-008`,
  `invoice/v1`) sat exactly on the boundary.
- `Munchen` read as `München` stays **scored wrong**: the model altered printed text.
- The new check still accepts a truncation at a word boundary of a multi-word value.
- "With the fixed check" was chosen after seeing "as run"; it was specified from three known cases, and the other results were not
  used to tune it. One run of each prompt, 30 synthetic documents, labels by construction, one model.
