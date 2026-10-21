# Prompt versions

The extraction prompt of a document type is a versioned constant in `app/extractors/<type>.py`; its version string is stored
with every extraction (`extractions.prompt_version`) and every evaluation run. A change to a prompt is a **new version, never an
edit**: `tests/test_prompts.py` pins `invoice/v1` byte for byte.

| Version | Written | Status |
|---|---|---|
| `invoice/v1` | the original | still selectable (`DOCINTEL_INVOICE_PROMPT_VERSION=invoice/v1`) |
| `invoice/v2` | 2026-10-21, from the first real run's failures on `invoices_v1`, **committed before any run that uses it** | **the default**, because it met every condition of the rule in `docs/EVAL.md` on the runs of 2026-10-21 (below); one run each, so read the caveats |
| `purchase_order/v1` | the original | the only purchase-order prompt |

## invoice/v2: what the evidence was, and what changed

The evidence is the first real run (`invoices_v1`, 20 documents, `invoice/v1`, `claude-haiku-4-5`, 2026-10-21) and **nothing else**:
the hard set was run with `invoice/v1` as well, and its per-document results were **not read** before this version was
committed.

Of 260 fields, 257 matched. The three misses are all `vendor_tax_id`, all on the same vendor (Meridian Logistics SA, the same
printed VAT number, `FR40123456824`, on four invoices, three of them transcribed wrongly):

| Document | The model wrote | What went wrong | Reported confidence | Flagged for review |
|---|---|---|---|---|
| invoice-005 | `FR4012345682` | the last digit dropped | 0.90 | **no** (final score 0.933) |
| invoice-010 | `FR4012345824` | a digit in the middle dropped | 0.90 | yes (final score 0.600) |
| invoice-015 | `FR4012345` + a zero-width space (U+200B) + `6824` | an invisible character inserted | 0.90 | **no** (final score 0.933) |
| invoice-020 | `FR40123456824` | correct | 0.99 | no |

So: the model copied a long digit run wrongly, from an identical printed string, three times in four, and reported 0.90 each
time. Every other field of every document was right, and the other identifiers (invoice numbers, purchase orders, the other
four vendors' tax ids) were copied correctly.

`invoice/v2` is `invoice/v1` plus **one paragraph**, placed after the `invoice_number` note, and nothing else changed:

> Identifiers (`invoice_number`, `purchase_order_number` and `vendor_tax_id`) are copied exactly as printed, one character at a time and in
> the same order. Do not drop, add, swap or merge a character, do not reformat or correct them, and do not insert spaces or invisible
> characters (such as a zero-width space). Long runs of digits, such as a VAT or tax number, are where copying goes wrong: once you have
> written one, check that it has the same number of characters as on the page. A confidence of 0.9 or more means you checked every
> character; if you could not, report less.

It does not touch the schema, the checker, the scoring or the review threshold.

## What this revision is, and is not (stated before it is run)

- **It is a hypothesis from three misses in one field.** The failure was variable (the same string came out right once and wrong
  three times), so a gain on a second run is as consistent with chance as with the prompt: the adapter does not set a
  temperature (the API default applies) and each document was run once.
- **It is in-sample for `invoices_v1`**, because it was written from that run's failures, and **out-of-sample only for
  `invoices_hard_v1`** (whose results were not read).
- **Two things it does not fix**, found while analysing the same three rows, left alone on purpose so that the comparison
  between v1 and v2 is not confounded by a code change:
  1. The text-layer check passed a **truncated** value and a value with an **invisible character** (both gave `found`): its
     matching is a substring match after dropping spaces and punctuation, so a prefix of the printed string, or the string
     with a zero-width space inside, still counts as "on the page". Two of the three misses were therefore not flagged. This is
     a defect of the scorer, reported in the results and to be fixed after the study.
  2. The model's self-reported confidence (0.90) was the same on the right and the wrong copies.

## What happened when v2 was run (2026-10-21; full record in `docs/results/2026-10-21-real-runs.md`)

| Over the same 30 documents (390 fields) | v1 | v2 |
|---|---|---|
| Wrong fields | 6 | 3 |
| Wrong and not flagged (false-confident) | 4 | 2 |
| `invoices_v1` alone (20 documents, 260 fields) | 257 correct | 260 correct |
| `invoices_hard_v1` alone (10 documents, 130 fields) | 127 correct | 127 correct |

By the rule written beforehand, v2 is the default. **All of the difference is on `invoices_v1`, the set v2 was written from.** On
the hard set, where v2 was not tuned, v1 and v2 each missed the same three fields. One run each at default sampling, so the
`invoices_v1` difference is as consistent with chance as with the prompt. The scorer weaknesses listed above were not fixed
for these runs and remain open.
