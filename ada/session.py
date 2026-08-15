"""Conversation history that survives a restart.

Tier 1 deliberately reset the transcript on every process start, and Tier 4
layered durable *facts* on top of that — but the conversation itself still
died with the process, so "what did we decide yesterday?" had no answer.
This keeps the recent transcript on disk between runs, shared by all three
front ends (text, push-to-talk, wake word) so they're one continuous
conversation rather than three amnesiac ones.

Two things make this more than a `json.dump`:

1. Assistant turns hold SDK content-block *objects*, not plain dicts, so they
   have to be normalized before they can be written — and normalized down to
   the fields the API actually accepts, not every field the SDK models carry.

2. History can't be trimmed at an arbitrary point. A `tool_use` block must be
   answered by a `tool_result` in the very next message; cutting between them
   hands the API a dangling tool call and it rejects the entire request. So a
   trim only ever lands on a boundary where a real user message starts.
"""

from typing import Any

from ada import store

# Roughly a few dozen exchanges. Deliberately a message count, not a token
# count: it's the cheap, predictable version, and the honest next step here is
# summarizing the dropped prefix rather than tuning this number.
MAX_MESSAGES = 60

_STORE = store.DATA_DIR / "session.json"
_lock = store.lock_for(_STORE)


def _block_type(block: Any) -> str | None:
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _block_to_dict(block: Any) -> dict:
    """One content block as a plain dict the API will accept back verbatim."""
    if isinstance(block, dict):
        return block

    kind = _block_type(block)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}

    # Anything else the SDK grows later: keep it, minus the null fields the
    # models pad out, which the API rejects on the way back in.
    dumped = block.model_dump() if hasattr(block, "model_dump") else dict(block)
    return {k: v for k, v in dumped.items() if v is not None}


def to_plain(messages: list[dict]) -> list[dict]:
    """The message list with every SDK object flattened to plain JSON."""
    plain = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            plain.append({"role": message["role"], "content": content})
        else:
            plain.append({"role": message["role"], "content": [_block_to_dict(b) for b in content]})
    return plain


def _is_turn_start(message: dict) -> bool:
    """True if `message` can legally be the first message of a request.

    That means a user message that isn't the tool_result half of a pair — a
    transcript starting on a tool_result is missing the tool_use it answers.
    """
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return not any(_block_type(b) == "tool_result" for b in content)


def trim(messages: list[dict], max_messages: int = MAX_MESSAGES) -> list[dict]:
    """The tail of `messages`, cut only at a legal turn boundary."""
    if len(messages) <= max_messages:
        return list(messages)

    for i in range(len(messages) - max_messages, len(messages)):
        if _is_turn_start(messages[i]):
            return messages[i:]

    # The whole tail is one unbroken tool chain with no user turn to cut at.
    # Starting fresh beats persisting something the API will reject.
    return []


def load() -> list[dict]:
    """The persisted transcript, or an empty list on a first run."""
    with _lock:
        messages = store.read_json(_STORE, [])
    if not isinstance(messages, list):
        return []
    valid = [m for m in messages if isinstance(m, dict) and "role" in m and "content" in m]
    return trim(valid)


def record(messages: list[dict]) -> None:
    """Trim `messages` in place to the cap, then persist it.

    In place on purpose: the caller's live list is the same one handed to the
    model next turn, so trimming a copy would cap the file while letting the
    in-memory context grow forever.
    """
    trimmed = trim(messages)
    if len(trimmed) != len(messages):
        messages[:] = trimmed
    with _lock:
        store.write_json(_STORE, to_plain(messages))


def clear() -> None:
    """Forget the transcript. Durable memory facts are untouched — those live
    in `data/memory.json` and are a separate thing to forget."""
    with _lock:
        store.write_json(_STORE, [])


def resumed_summary(messages: list[dict]) -> str | None:
    """A one-liner for startup, or None when there's nothing to resume."""
    if not messages:
        return None
    exchanges = sum(1 for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str))
    if not exchanges:
        return None
    plural = "s" if exchanges != 1 else ""
    return f"(Picking up our last conversation — {exchanges} exchange{plural} back. Say 'reset' to start fresh.)"
