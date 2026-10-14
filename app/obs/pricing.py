"""Rough cost estimates, in US dollars per million tokens. Edit when Bedrock prices change.

These are estimates for trend-spotting, not an invoice: the cost is computed when a span is recorded
and stored with it, so changing a price here does not rewrite history.
"""

# (substring of the model id, input $/1M tokens, output $/1M tokens), first match wins
PRICES: list[tuple[str, float, float]] = [
    ("opus", 15.0, 75.0),
    ("sonnet", 3.0, 15.0),
    ("haiku", 1.0, 5.0),
    ("titan-embed", 0.02, 0.0),
]


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float:
    name = (model or "").lower()
    for needle, price_in, price_out in PRICES:
        if needle in name:
            return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    return 0.0
