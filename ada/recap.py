"""Turning the part of a conversation that falls off the end into a paragraph.

`session.py` used to drop the old prefix outright, which is the cheap version
and loses real things: what the user asked for, what was decided, what they
told you about themselves in passing. Durable *facts* were already safe —
`remember` puts those in `data/memory.json` — but a fact has to be noticed to
be remembered, and most of what matters in a long conversation was never
noticed, just said.

So the prefix is summarized instead of discarded, and the summary rides along
at the front of the transcript. Because it sits at the front, the *next* trim
summarizes the summary along with whatever else fell off — the recap rolls
forward rather than accumulating one block per trim.

**It fails soft, always.** A summary that can't be produced — the provider is
down, the budget is spent — must not take the conversation down with it. The
fallback is exactly the old behaviour: drop the prefix and carry on. Losing
context is a bad outcome; refusing to talk is a worse one.
"""

from ada import audit
from ada.llm import LLMError, complete

# Long enough to keep the shape of a conversation, short enough that it can't
# quietly become the bulk of every request once it's riding at the front.
MAX_SUMMARY_TOKENS = 400

MARKER = "[Earlier in this conversation]"

_INSTRUCTION = """Below is the earlier part of a conversation between a user and their \
personal assistant, which is about to fall out of the assistant's context window.

Write a compact summary of it so the assistant can carry on without having read it. \
Keep: what the user asked for and whether it happened, decisions and preferences they \
expressed, anything they said about themselves or their situation, and anything left \
unfinished. Drop pleasantries, tool mechanics, and anything already superseded later in \
the excerpt.

Write it as notes in the third person about the user, not as dialogue. No preamble, no \
"here is a summary" — just the notes.

--- conversation ---
{transcript}
--- end ---"""


def _render(messages: list[dict]) -> str:
    """The messages as plain text a model can read.

    Tool traffic is reduced to a mention rather than reproduced: what a search
    returned matters far less than what was concluded from it, and pasting
    whole tool results back in is how a summary ends up longer than the thing
    it summarizes.
    """
    lines = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content")

        if isinstance(content, str):
            lines.append(f"{role}: {content}")
            continue
        if not isinstance(content, list):
            continue

        for block in content:
            kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if kind == "text":
                text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
                if text:
                    lines.append(f"{role}: {text}")
            elif kind == "tool_use":
                name = block.get("name") if isinstance(block, dict) else getattr(block, "name", "?")
                lines.append(f"{role}: (used the {name} tool)")
    return "\n".join(lines)


def summarize(messages: list[dict]) -> str | None:
    """A paragraph standing in for `messages`, or None if one can't be had."""
    transcript = _render(messages)
    if not transcript.strip():
        return None

    try:
        summary = complete(_INSTRUCTION.format(transcript=transcript), max_tokens=MAX_SUMMARY_TOKENS)
    except LLMError as e:
        # The conversation carries on without it. See the module docstring:
        # losing context is bad, refusing to talk is worse.
        audit.log("recap_failed", error=str(e), messages=len(messages))
        print(f"\n[ada: couldn't summarize the older part of this conversation ({e}) — it's been dropped]", flush=True)
        return None

    return summary.strip() or None
