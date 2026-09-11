"""Formatting a report for a terminal."""

from evals.metrics import Report

WIDTH = 74


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:6.2f}%"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} ms"


def _usd(value: object) -> str:
    return "n/a" if value is None else f"${value}"


def render(report: Report, *, threshold: float, notes: list[str] | None = None) -> str:
    """The human-readable report the CLI prints."""
    lines: list[str] = []
    add = lines.append

    add("=" * WIDTH)
    add(f"Evaluation: {report.dataset_name}")
    add("=" * WIDTH)
    add(f"  model            {report.model_name or 'n/a'}")
    add(f"  prompt version   {report.prompt_version or 'n/a'}")
    add(f"  documents        {report.documents}")
    add(f"  fields evaluated {report.fields_evaluated}")
    add(f"  review threshold {threshold:.2f}")

    for note in notes or []:
        add(f"  ! {note}")

    add("")
    add("Exact-match accuracy per field")
    add(f"  {'field':<24}{'evaluated':>10}{'correct':>9}{'accuracy':>11}")
    add(f"  {'-' * 54}")
    for name in sorted(report.field_accuracy):
        accuracy = report.field_accuracy[name]
        add(
            f"  {name:<24}{accuracy.evaluated:>10}{accuracy.correct:>9}"
            f"{_pct(accuracy.accuracy):>11}"
        )
    add(f"  {'-' * 54}")
    add(
        f"  {'overall':<24}{report.fields_evaluated:>10}"
        f"{report.correct_confident + report.correct_flagged:>9}"
        f"{_pct(report.overall_accuracy):>11}"
    )

    add("")
    add("Outcomes (every evaluated field falls in exactly one)")
    add(f"  correct, not flagged   {report.correct_confident:>6}")
    add(f"  correct, flagged       {report.correct_flagged:>6}")
    add(f"  wrong, flagged         {report.wrong_flagged:>6}   (caught)")
    add(f"  wrong, NOT flagged     {report.wrong_confident:>6}   (false-confident)")

    add("")
    add("Rates")
    add(
        f"  review rate            {_pct(report.review_rate)}"
        f"   ({report.fields_flagged}/{report.fields_evaluated} fields)"
    )
    add(
        f"  document review rate   {_pct(report.document_review_rate)}"
        f"   ({report.documents_flagged}/{report.documents} documents)"
    )
    add(
        f"  FALSE-CONFIDENT RATE   {_pct(report.false_confident_rate)}"
        f"   ({report.wrong_confident}/{report.fields_evaluated} fields)"
    )
    add(
        f"    of unflagged fields  {_pct(report.false_confident_rate_of_unflagged)}"
        f"   ({report.wrong_confident}/"
        f"{report.correct_confident + report.wrong_confident} fields)"
    )

    add("")
    add("Cost")
    add(f"  mean per document      {_usd(report.mean_cost_usd)}")
    add(f"  total                  {_usd(report.total_cost_usd)}")
    add(
        f"  documents with cost    {report.documents_with_cost}/{report.documents}"
    )
    if report.mean_cost_usd is None:
        add("  not available — the provider reported no usage for any document")

    add("")
    add("Latency (provider call only: request sent to full response received)")
    add(f"  mean                   {_ms(report.mean_latency_ms)}")
    add(f"  p50                    {_ms(report.p50_latency_ms)}")
    add(f"  p95                    {_ms(report.p95_latency_ms)}")
    add(
        f"  documents with latency {len(report.latencies_ms)}/{report.documents}"
    )
    if not report.latencies_ms:
        add("  not available — no document recorded a latency")

    add("")
    if report.failed_documents:
        add(f"FAILURES ({len(report.failed_documents)})")
        for document_id in report.failed_documents:
            add(f"  {document_id}")
        add("  Their labelled fields are counted as wrong, not skipped.")
    else:
        add("No document failed to extract.")
    add("=" * WIDTH)

    return "\n".join(lines)
