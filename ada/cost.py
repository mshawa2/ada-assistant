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

# Cached input isn't billed at the input rate. Writing an entry costs a premium
# over sending the tokens plainly; reading one back is most of the point. Both
# are multipliers on the input price rather than prices of their own, because
# that's how they're published — so they follow the input rate when it changes.
DEFAULT_CACHE_WRITE_MULTIPLIER = 1.25
DEFAULT_CACHE_READ_MULTIPLIER = 0.10

_lock = threading.Lock()
_input_tokens = 0
_output_tokens = 0
_cache_read_tokens = 0
_cache_write_tokens = 0


def price_of(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimated cost of one call, in the configured currency.

    The cache counts default to zero, so a caller that knows nothing about
    caching still prices an uncached call correctly.
    """
    input_price = settings.get("model.pricing.input_per_mtok", DEFAULT_INPUT_PRICE_PER_MTOK)
    output_price = settings.get("model.pricing.output_per_mtok", DEFAULT_OUTPUT_PRICE_PER_MTOK)
    read_rate = settings.get("model.pricing.cache_read_multiplier", DEFAULT_CACHE_READ_MULTIPLIER)
    write_rate = settings.get("model.pricing.cache_write_multiplier", DEFAULT_CACHE_WRITE_MULTIPLIER)

    # `input_tokens` from the API counts only what was billed at full rate —
    # cached tokens are reported separately, so these add rather than overlap.
    billed_input = input_tokens + cache_read_tokens * read_rate + cache_write_tokens * write_rate
    return (billed_input / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


def record(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    global _input_tokens, _output_tokens, _cache_read_tokens, _cache_write_tokens
    with _lock:
        _input_tokens += input_tokens
        _output_tokens += output_tokens
        _cache_read_tokens += cache_read_tokens
        _cache_write_tokens += cache_write_tokens

    # Outside the lock: the ledger takes its own, and holding both at once is
    # how two locks become a deadlock later.
    note = f"{input_tokens} in / {output_tokens} out"
    if cache_read_tokens or cache_write_tokens:
        note += f" (+{cache_read_tokens} cached, {cache_write_tokens} written)"
    budget.record(
        budget.MODEL,
        price_of(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens),
        note=note,
    )


def totals() -> dict:
    """This session's tokens and what they're estimated to have cost."""
    with _lock:
        input_tokens, output_tokens = _input_tokens, _output_tokens
        cache_read, cache_write = _cache_read_tokens, _cache_write_tokens
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cached": cache_read,
        "written": cache_write,
        "cost": price_of(input_tokens, output_tokens, cache_read, cache_write),
    }


def summary() -> str | None:
    counted = totals()
    input_tokens, output_tokens = counted["input"], counted["output"]
    if not input_tokens and not output_tokens and not counted["cached"]:
        return None

    line = (
        f"Session usage: {input_tokens:,} input / {output_tokens:,} output tokens "
        f"(~{budget.money(counted['cost'])}, approximate)"
    )
    if counted["cached"] or counted["written"]:
        # Worth saying out loud rather than burying: this is the number that
        # shows whether the cache is earning its keep.
        line += (
            f"\n{counted['cached']:,} tokens read from cache at a tenth of the rate, "
            f"{counted['written']:,} written to it"
        )
    running = budget.summary()
    return f"{line}\n{running}" if "No spending limits" not in running else line
