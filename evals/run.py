"""The evaluation CLI: `python -m evals.run --dataset invoices_v1`.

Runs a labelled dataset through the real pipeline, prints a report, and records
the run in `eval_runs`. Exits non-zero if any document failed to extract, so a
broken run cannot be mistaken for a good one.
"""

import argparse
import json
import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.providers.pricing import price_for
from evals import dataset as dataset_module
from evals import providers, runner
from evals import report as report_module
from evals.budget import DEFAULT_LEDGER, BudgetedProvider, RunBudget, RunStopped

EXIT_OK = 0
EXIT_FAILED_DOCUMENTS = 1
EXIT_BAD_USAGE = 2
EXIT_STOPPED = 3


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
        default=providers.STUB,
        help=(
            "stub (default) runs offline and measures the harness, not extraction quality. "
            "anthropic calls the real API and costs money: it needs --request-cap and --max-cost-usd."
        ),
    )
    parser.add_argument(
        "--request-cap",
        type=int,
        default=None,
        help="anthropic only: the most model requests this whole piece of work may make (kept in the ledger).",
    )
    parser.add_argument(
        "--max-cost-usd",
        default=None,
        help="anthropic only: the most this whole piece of work may spend, in USD (kept in the ledger).",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help="anthropic only: where the request and cost ledger is kept (default var/eval_ledger.json).",
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


def _real_run_problem(args: argparse.Namespace, settings) -> str | None:
    """Why a real (billed) run may not start, or None. Checked before anything is sent."""
    if args.request_cap is None or args.max_cost_usd is None:
        return "--provider anthropic costs money: give both --request-cap N and --max-cost-usd X."
    if args.request_cap < 1:
        return "--request-cap must be at least 1."
    try:
        if Decimal(args.max_cost_usd) <= 0:
            return "--max-cost-usd must be more than 0."
    except InvalidOperation:
        return f"--max-cost-usd {args.max_cost_usd!r} is not a number."
    if settings.extraction_max_retries != 0:
        return (
            "a counted run needs DOCINTEL_EXTRACTION_MAX_RETRIES=0: a retry inside the SDK is a request "
            "the ledger cannot see."
        )
    if price_for(settings.extraction_model) is None:
        return (
            f"no price is known for {settings.extraction_model!r}, so the cost cap could not be enforced; "
            "add one with DOCINTEL_PRICING_USD_PER_MTOK."
        )
    return None


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

    settings = get_settings()
    budget: RunBudget | None = None
    if args.provider == providers.ANTHROPIC:
        problem = _real_run_problem(args, settings)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_BAD_USAGE
        budget = RunBudget(
            request_cap=args.request_cap,
            cost_cap_usd=Decimal(args.max_cost_usd),
            path=args.ledger,
        )
        print(
            f"Request cap {budget.request_cap}, cost cap ${budget.cost_cap_usd}; "
            f"{budget.requests_used} request(s) and ${budget.spent_usd} already used in {budget.path}. "
            f"This run needs up to {len(loaded.documents)} request(s).",
            flush=True,
        )
        provider = BudgetedProvider(providers.build(args.provider), budget)
    else:
        provider = providers.build(args.provider)

    notes: list[str] = []
    if loaded.synthetic:
        notes.append(
            "Dataset is synthetic; these numbers are not real-world accuracy."
        )
    if args.provider == providers.STUB:
        notes.append(providers.STUB_WARNING)

    with get_sessionmaker()() as session:
        try:
            result = runner.evaluate(session, loaded, provider)
        except RunStopped as stopped:
            print(
                f"\nStopped after {stopped.documents_done} of {stopped.documents_total} document(s): "
                f"{stopped.reason}\nNothing was retried and no eval_runs row was written: a partial run is "
                "not a result. The documents tried are in the database, each failed attempt with its reason.",
                file=sys.stderr,
            )
            if budget is not None:
                print(
                    f"Requests used so far: {budget.requests_used} of {budget.request_cap}; "
                    f"spent ${budget.spent_usd} of ${budget.cost_cap_usd}.",
                    file=sys.stderr,
                )
            return EXIT_STOPPED

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

    if budget is not None:
        print(
            f"\nRequests used in total: {budget.requests_used} of {budget.request_cap}; "
            f"spent ${budget.spent_usd} of ${budget.cost_cap_usd}."
        )

    if result.failed_documents:
        print(
            f"\n{len(result.failed_documents)} document(s) failed to extract.",
            file=sys.stderr,
        )
        return EXIT_FAILED_DOCUMENTS

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
