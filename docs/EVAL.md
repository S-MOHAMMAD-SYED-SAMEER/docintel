# Evaluation: the plan for the first real-model runs, written before any of them

**Status: no real-model request has been made.** This file is committed first, so the plan and the decision rule cannot
be bent to a result. When the runs have been made, their numbers go in `docs/results/` and are summarised in the README
with their limits; this file keeps the plan as it was written.

What is measured and how is in the README ("Evaluation", "Metric definitions"). This file says **what will be run, with
what caps, in what order, and how the one prompt revision will be decided.**

## Model and settings

- Model: `claude-haiku-4-5` (the default in `app/config.py`). Its price (USD 1 per million input tokens, 5 per million output
  tokens) was checked against Anthropic's pricing page on 2026-10-20 (`app/providers/pricing.py`). The model ID itself is
  **not yet verified against the live API**: the first request is that check.
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
| `invoices_hard_v1` | 10 | Synthetic invoices that are harder: five images with no text layer (a scan, a photograph, a stamp and a scribble), skew, blur, other layouts, European formats, a credit note, a missing due date, a two-page table | **drafted, in review; frozen (sha256) before any real run** |
| `purchase_orders_smoke` | 3 | Proves the second document type runs end to end; not a benchmark | committed |

Provenance, stated wherever a number from the hard set is quoted: *written by Claude (generated; the labels are the render
inputs); reviewed by ChatGPT and Claude; final decisions by Sameer.* The hard set's labels are never changed after a model
has seen the documents; a change would be a new dataset version.

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
