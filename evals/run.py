"""The evaluation CLI: `python -m evals.run --dataset invoices_v1`.

Runs a labelled dataset through the real pipeline, prints a report, and records
the run in `eval_runs`. Exits non-zero if any document failed to extract, so a
broken run cannot be mistaken for a good one.
"""

import argparse
import json
import logging
import sys

from app.config import get_settings
from app.db.session import get_sessionmaker
from evals import dataset as dataset_module
from evals import providers, report as report_module, runner

EXIT_OK = 0
EXIT_FAILED_DOCUMENTS = 1
EXIT_BAD_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Evaluate extraction against a labelled dataset.",
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Dataset under evals/datasets/, e.g. invoices_v1.",
    )
    parser.add_argument(
        "--provider",
        choices=providers.PROVIDER_CHOICES,
        default=providers.ANTHROPIC,
        help=(
            "anthropic (default) calls the real API and costs money. stub runs "
            "offline and measures the harness, not extraction quality."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Evaluate only the first N documents (for a quick check).",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Print the report without writing an eval_runs row.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print the report as JSON on stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    try:
        loaded = dataset_module.load(args.dataset)
    except dataset_module.DatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE

    if args.limit is not None:
        if args.limit < 1:
            print("error: --limit must be at least 1", file=sys.stderr)
            return EXIT_BAD_USAGE
        loaded = dataset_module.Dataset(
            name=loaded.name,
            directory=loaded.directory,
            synthetic=loaded.synthetic,
            description=loaded.description,
            documents=loaded.documents[: args.limit],
        )

    provider = providers.build(args.provider)
    settings = get_settings()

    notes: list[str] = []
    if loaded.synthetic:
        notes.append(
            "Dataset is synthetic; these numbers are not real-world accuracy."
        )
    if args.provider == providers.STUB:
        notes.append(providers.STUB_WARNING)

    with get_sessionmaker()() as session:
        result = runner.evaluate(session, loaded, provider)

        if not args.no_persist:
            runner.persist(
                session,
                result,
                model_name=result.model_name or getattr(
                    provider, "model_name", settings.extraction_model
                ),
                prompt_version=result.prompt_version or "unknown",
            )

    print(
        report_module.render(
            result, threshold=settings.confidence_threshold, notes=notes
        )
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))

    if result.failed_documents:
        print(
            f"\n{len(result.failed_documents)} document(s) failed to extract.",
            file=sys.stderr,
        )
        return EXIT_FAILED_DOCUMENTS

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
