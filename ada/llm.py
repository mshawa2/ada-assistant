from collections.abc import Iterator

import anthropic

from ada import audit, cost
from ada.config import ANTHROPIC_API_KEY, MODEL
from ada.system_prompt import build_system_prompt

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# 1024 was enough for chat-length replies but silently truncated the long ones
# — a summarized web page, a digest of several notes. `agent.py` now says so
# out loud when a reply is cut off; this raises the ceiling so it rarely is.
MAX_TOKENS = 4096


class LLMError(Exception):
    """Raised when the model provider can't be reached or fails."""


def stream_turn(messages: list[dict], tools: list[dict]) -> Iterator[str]:
    """Send one turn to the model and yield reply text as it streams in.

    This is the only place in the codebase that touches the Anthropic SDK —
    everything else calls this function. The generator returns (via
    StopIteration.value) the final Message, which the caller inspects for
    tool_use blocks and stop_reason.
    """
    try:
        with _client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system_prompt(),
            messages=messages,
            tools=tools,
        ) as stream:
            yield from stream.text_stream
            final_message = stream.get_final_message()
            cost.record(final_message.usage.input_tokens, final_message.usage.output_tokens)
            audit.log(
                "model_call",
                stop_reason=final_message.stop_reason,
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
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
