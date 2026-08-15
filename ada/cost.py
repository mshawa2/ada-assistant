"""A running tally of model token usage / estimated cost for the current
session — the audit trail's bonus feature. Pricing is Claude Sonnet 5's
current published rate (intro pricing through 2026-08-31); if it's stale by
the time you read this, the token counts are still exact even if the dollar
estimate isn't.
"""

import threading

# Per million tokens, Claude Sonnet 5 (claude-sonnet-5), intro pricing.
_INPUT_PRICE_PER_MTOK = 2.00
_OUTPUT_PRICE_PER_MTOK = 10.00

_lock = threading.Lock()
_input_tokens = 0
_output_tokens = 0


def record(input_tokens: int, output_tokens: int) -> None:
    global _input_tokens, _output_tokens
    with _lock:
        _input_tokens += input_tokens
        _output_tokens += output_tokens


def summary() -> str | None:
    with _lock:
        input_tokens, output_tokens = _input_tokens, _output_tokens
    if not input_tokens and not output_tokens:
        return None
    cost = (input_tokens / 1_000_000) * _INPUT_PRICE_PER_MTOK + (
        output_tokens / 1_000_000
    ) * _OUTPUT_PRICE_PER_MTOK
    return (
        f"Session usage: {input_tokens:,} input / {output_tokens:,} output tokens "
        f"(~${cost:.4f}, approximate)"
    )
