"""A running tally of model token usage and estimated cost.

Two audiences, deliberately kept apart:

- **This session**, printed on exit. Token counts are exact — straight from
  the API's `usage` field — while the dollar figure is an estimate, because
  published prices change and this file can go stale.
- **Across sessions**, via `ada/budget.py`. The in-memory tally here dies with
  the process, which meant "how much has Ada cost me this month" had no answer
  and nothing stopped a bad week. Every call now also lands in the ledger,
  where a cap can be enforced.

Pricing lives in settings (`model.pricing`) rather than being hardcoded, so
correcting it — or switching to a model priced differently — is a config edit,
not a code change.
"""

import threading

from ada import budget, settings

# Fallbacks only, used when nothing is configured: Claude Sonnet 5's published
# rate per million tokens at time of writing.
DEFAULT_INPUT_PRICE_PER_MTOK = 2.00
DEFAULT_OUTPUT_PRICE_PER_MTOK = 10.00

_lock = threading.Lock()
_input_tokens = 0
_output_tokens = 0


def price_of(input_tokens: int, output_tokens: int) -> float:
    """Estimated cost of one call, in the configured currency."""
    input_price = settings.get("model.pricing.input_per_mtok", DEFAULT_INPUT_PRICE_PER_MTOK)
    output_price = settings.get("model.pricing.output_per_mtok", DEFAULT_OUTPUT_PRICE_PER_MTOK)
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


def record(input_tokens: int, output_tokens: int) -> None:
    global _input_tokens, _output_tokens
    with _lock:
        _input_tokens += input_tokens
        _output_tokens += output_tokens

    # Outside the lock: the ledger takes its own, and holding both at once is
    # how two locks become a deadlock later.
    budget.record(
        budget.MODEL,
        price_of(input_tokens, output_tokens),
        note=f"{input_tokens} in / {output_tokens} out",
    )


def summary() -> str | None:
    with _lock:
        input_tokens, output_tokens = _input_tokens, _output_tokens
    if not input_tokens and not output_tokens:
        return None

    line = (
        f"Session usage: {input_tokens:,} input / {output_tokens:,} output tokens "
        f"(~{budget.money(price_of(input_tokens, output_tokens))}, approximate)"
    )
    running = budget.summary()
    return f"{line}\n{running}" if "No spending limits" not in running else line
