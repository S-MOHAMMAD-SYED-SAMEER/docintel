"""Re-score the saved model outputs of a real run with a different text-layer check. No requests are made.

  python -m evals.rescore                      # writes docs/results/2026-10-21-rescored.json
  python -m evals.rescore --check              # fails if the file on disk differs from what this would write

The model's replies, the extracted values and the labels are read from the results files
(`docs/results/<date>-<dataset>-<prompt>.json`) and are not changed. Only the text-layer signal is recomputed, from the
dataset's own document files, with the check that now ships (`app.validation.text_layer`); the other signals, the weights
and the threshold are taken from the saved record. The check that was in use when the runs were made (a substring
match after dropping everything that is not a letter or a digit) is kept here, as `legacy_contains`, so that the
"as run" side can be recomputed and compared with what was saved: if it does not reproduce the saved flags exactly,
nothing is published.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from app import rendering
from app.media import detect
from app.validation.text_layer import TextLayer
from evals import dataset

RESULTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "results"
DATE = "2026-10-21"
RUNS = (
    ("invoices_v1", "invoice-v1"),
    ("invoices_v1", "invoice-v2"),
    ("invoices_hard_v1", "invoice-v1"),
    ("invoices_hard_v1", "invoice-v2"),
    ("purchase_orders_smoke", "purchase-order-v1"),
)
OUTPUT = RESULTS_DIR / f"{DATE}-rescored.json"
TEXT_LAYER = "text_layer"
_LEGACY_NOISE = re.compile(r"[^0-9a-z]+")


def legacy_contains(pages: tuple[str, ...], value: str) -> bool:
    """The check used when the runs were made: substring after dropping every character but 0-9 and a-z."""
    needle = _LEGACY_NOISE.sub("", value.casefold())
    if not needle:
        return False
    return any(needle in _LEGACY_NOISE.sub("", page.casefold()) for page in pages)


def _weights(meta: dict[str, Any]) -> dict[str, float]:
    return dict(meta["confidence_weights"])


def _combine(signals: list[tuple[str, float]], weights: dict[str, float]) -> float:
    total = sum(weights[name] for name, _ in signals)
    return sum(weights[name] * score for name, score in signals) / total


def _outcome(correct: bool, flagged: bool) -> str:
    if correct:
        return "correct_flagged" if flagged else "correct_confident"
    return "wrong_flagged" if flagged else "wrong_confident"


def _text_layer(directory: Path, filename: str) -> TextLayer:
    path = directory / filename
    media = detect(path.read_bytes())
    return TextLayer.from_pages(rendering.extract_text_layer(path, media))


def _rescore_field(
    field: dict[str, Any], layer: TextLayer, weights: dict[str, float], threshold: float, matcher: str
) -> tuple[float, bool, str | None]:
    """The confidence, the flag and the text-layer verdict ("found", "not found" or None) under `matcher`."""
    signals = [(s["name"], s["score"]) for s in field["signals"]]
    verdict = None
    if any(name == TEXT_LAYER for name, _ in signals):
        value = field["extracted"]
        text = value if isinstance(value, str) else str(value)
        found = layer.contains(text) if matcher == "fixed" else legacy_contains(layer.pages, text)
        verdict = "found" if found else "not found"
        signals = [(name, (1.0 if found else 0.0) if name == TEXT_LAYER else score) for name, score in signals]
    confidence = _combine(signals, weights)
    return confidence, confidence < threshold or bool(field["failed_checks"]), verdict


def _empty_counts() -> dict[str, int]:
    return {"correct_confident": 0, "correct_flagged": 0, "wrong_flagged": 0, "wrong_confident": 0}


def rescore_run(dataset_name: str, slug: str) -> dict[str, Any]:
    path = RESULTS_DIR / f"{DATE}-{dataset_name}-{slug}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    meta = record["meta"]
    weights, threshold = _weights(meta), meta["confidence_threshold"]
    directory = dataset.load(dataset_name).directory

    as_run, fixed = _empty_counts(), _empty_counts()
    changes: list[dict[str, Any]] = []
    flagged_fields = {"as_run": 0, "fixed": 0}
    evaluated = 0
    for document in record["documents"]:
        layer = _text_layer(directory, document["filename"])
        for name, field in document["fields"].items():
            evaluated += 1
            old_conf, old_flag, old_verdict = _rescore_field(field, layer, weights, threshold, "legacy")
            # The saved record must be reproduced exactly before anything new is said.
            if old_flag != field["needs_review"] or abs(old_conf - field["confidence"]) > 1e-9:
                raise SystemExit(
                    f"{path.name} {document['filename']} {name}: the 'as run' check does not reproduce the saved "
                    f"record (saved flag {field['needs_review']}, confidence {field['confidence']}; "
                    f"recomputed {old_flag}, {old_conf}). Nothing written."
                )
            new_conf, new_flag, new_verdict = _rescore_field(field, layer, weights, threshold, "fixed")
            as_run[_outcome(field["correct"], old_flag)] += 1
            fixed[_outcome(field["correct"], new_flag)] += 1
            flagged_fields["as_run"] += old_flag
            flagged_fields["fixed"] += new_flag
            if old_verdict != new_verdict or old_flag != new_flag:
                changes.append(
                    {
                        "filename": document["filename"],
                        "field": name,
                        "expected": field["expected"],
                        "extracted": field["extracted"],
                        "correct": field["correct"],
                        "text_layer_as_run": old_verdict,
                        "text_layer_fixed": new_verdict,
                        "confidence_as_run": round(old_conf, 4),
                        "confidence_fixed": round(new_conf, 4),
                        "flagged_as_run": old_flag,
                        "flagged_fixed": new_flag,
                    }
                )
    return {
        "run": meta["run"],
        "dataset": dataset_name,
        "prompt_version": meta["prompt_version"],
        "fields_evaluated": evaluated,
        "as_run": {**as_run, "flagged": flagged_fields["as_run"]},
        "with_fixed_check": {**fixed, "flagged": flagged_fields["fixed"]},
        "fields_whose_text_layer_verdict_or_flag_changed": changes,
    }


def build() -> dict[str, Any]:
    return {
        "what": (
            "The saved model outputs of the real runs of 2026-10-21, re-scored offline (no requests) with the "
            "text-layer check that now ships. Model replies, extracted values and labels are unchanged; only the "
            "text-layer signal is recomputed, from the dataset's own files. 'as_run' is the check that was in use "
            "when the runs were made, recomputed here and verified to reproduce every saved flag and confidence."
        ),
        "threshold": 0.85,
        "runs": [rescore_run(name, slug) for name, slug in RUNS],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the file on disk differs")
    args = parser.parse_args(argv)
    text = json.dumps(build(), indent=2, ensure_ascii=True) + "\n"
    if args.check:
        if OUTPUT.read_text(encoding="utf-8") != text:
            print(f"{OUTPUT.name} is out of date", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
