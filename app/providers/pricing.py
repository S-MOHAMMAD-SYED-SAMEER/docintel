"""What a call cost, from the model's published prices.

USD per million tokens, as `(input, output)`. **Hand-entered** from Anthropic's published pricing page
(https://platform.claude.com/docs/en/about-claude/pricing, read as raw HTML on `PRICES_AS_OF`): the page's
standard input and output prices, not the batch or cached ones. A model with no entry has no cost
(`None`), never a guess; an entry can be added or corrected with `DOCINTEL_PRICING_USD_PER_MTOK` (a JSON
object) without a code change. The lookup also accepts a dated snapshot of a listed model (the API returns
the resolved ID, for example `claude-haiku-4-5-20251001` for the alias `claude-haiku-4-5`) by matching the
longest listed ID that the name starts with.
"""

from decimal import Decimal

from app.config import get_settings

PRICES_AS_OF = "2026-10-20"

TOKENS_PER_MTOK = Decimal(1_000_000)

PRICING_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
    "claude-fable-5-1": (Decimal("10.00"), Decimal("50.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
}


def price_for(model: str) -> tuple[Decimal, Decimal] | None:
    """The `(input, output)` price per million tokens, or None when the model is not listed."""
    table = {**PRICING_USD_PER_MTOK, **get_settings().pricing_usd_per_mtok}
    if model in table:
        return table[model]
    matches = [known for known in table if model.startswith(known + "-")]
    return table[max(matches, key=len)] if matches else None


def estimate_cost_usd(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    """Cost of one call, or None when the model's price or the token counts are unknown."""
    pricing = price_for(model)
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    input_rate, output_rate = pricing
    cost = (
        Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
    ) / TOKENS_PER_MTOK
    # Six decimal places: a single page costs fractions of a cent.
    return cost.quantize(Decimal("0.000001"))
