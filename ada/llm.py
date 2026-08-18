from collections.abc import Iterator

import anthropic

from ada import audit, budget, cost, settings, system_prompt
from ada.config import ANTHROPIC_API_KEY

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Fallbacks only — the live values come from ada_config.json's `model` block,
# read per call so a change takes effect without a restart. 1024 was the old
# hardcoded cap and it silently truncated long replies (a summarized web page,
# a digest of several notes); `agent.py` now says so out loud when a reply is
# cut off, and the default ceiling is high enough that it rarely is.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096


def _system() -> list[dict]:
    """The system prompt, split at the cache breakpoint.

    Everything the provider bills is a prefix: tool schemas, then system, then
    the conversation. A cache breakpoint says "I will send these exact bytes
    again" — so it goes at the end of the last thing that doesn't move, and
    everything volatile goes after it.

    Here that's ~6,800 tokens above the line (the prompt plus whichever tool
    schemas routing picked) and one clock tick below it. Cached reads bill at a
    tenth of the rate, and a turn is two calls or more — the second one onward
    reads what the first one wrote.
    """
    return [
        {
            "type": "text",
            "text": system_prompt.stable_prompt(),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": system_prompt.now_line()},
    ]


class LLMError(Exception):
    """Raised when the model provider can't be reached or fails."""


def stream_turn(messages: list[dict], tools: list[dict]) -> Iterator[str]:
    """Send one turn to the model and yield reply text as it streams in.

    This is the only place in the codebase that touches the Anthropic SDK —
    everything else calls this function. The generator returns (via
    StopIteration.value) the final Message, which the caller inspects for
    tool_use blocks and stop_reason.
    """
    # Checked before the call, not after: discovering you're over budget once
    # the money is already spent isn't a limit, it's a receipt. The exact cost
    # of *this* call isn't knowable until it returns, so this asks the cheaper
    # question — are we already at the ceiling?
    try:
        budget.check(budget.MODEL)
    except budget.BudgetExceeded as e:
        raise LLMError(str(e)) from e

    try:
        with _client.messages.stream(
            model=settings.get("model.conversation", DEFAULT_MODEL),
            max_tokens=settings.get("model.max_tokens", DEFAULT_MAX_TOKENS),
            system=_system(),
            messages=messages,
            tools=tools,
        ) as stream:
            yield from stream.text_stream
            final_message = stream.get_final_message()
            usage = final_message.usage
            # Cached tokens are reported apart from `input_tokens`, which counts
            # only what was billed at full rate. Reading them back is also the
            # one honest way to know caching is working: a cache_read of 0 on
            # every call means something above the breakpoint is moving.
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cost.record(usage.input_tokens, usage.output_tokens, cache_read, cache_write)
            audit.log(
                "model_call",
                stop_reason=final_message.stop_reason,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            return final_message
    except anthropic.APIConnectionError as e:
        raise LLMError("Couldn't reach the model — check your network connection.") from e
    except anthropic.AuthenticationError as e:
        raise LLMError("Authentication failed — check your ANTHROPIC_API_KEY.") from e
    except anthropic.RateLimitError as e:
        # Caught ahead of APIStatusError (its parent) so a rate limit reads as
        # "wait a moment", not as an unexplained provider error.
        raise LLMError("Rate limited by the model provider — give it a moment and try again.") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"The model provider returned an error: {e.message}") from e
