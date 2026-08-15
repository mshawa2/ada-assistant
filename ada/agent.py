"""The shared agent core. Both the text REPL and the voice REPL drive a
conversation through `run_turn` — this is the one place tool-calling logic
lives, so voice and text can never drift apart."""

from collections.abc import Callable

from ada import audit, limits, safety, status
from ada.llm import stream_turn
from ada.tools import call as call_tool, specs as tool_specs
from ada.tools.registry import get as get_tool

MAX_TOOL_ROUNDS = 8


def run_turn(
    messages: list[dict],
    on_text: Callable[[str], None],
    on_status: Callable[[str], None],
    confirm: Callable[[str], bool],
) -> None:
    """Runs one full turn, including any tool calls, appending assistant/tool
    messages to `messages` in place.

    `on_text` is called with each chunk of the model's own reply text, in
    order, as it streams in — this is what a caller should print and/or
    speak. `on_status` is called with human-readable status lines (which
    tool is running) that are for the console only, never spoken aloud.
    `confirm` is called, with a plain-language description of the action,
    only for tools flagged in safety_config.json — it must return True before
    the tool actually runs.
    """
    final_message = _stream(messages, on_text, on_status)

    for _ in range(MAX_TOOL_ROUNDS):
        if final_message.stop_reason != "tool_use":
            messages.append({"role": "assistant", "content": final_message.content})
            return
        messages.append({"role": "assistant", "content": final_message.content})
        messages.append({"role": "user", "content": _execute_tool_calls(final_message, on_status, confirm)})
        final_message = _stream(messages, on_text, on_status)

    # Model kept calling tools past the round cap — close out the pending
    # tool_use blocks so the conversation stays valid, then stop.
    on_status("stopped after too many tool calls in a row")
    cancelled = [
        {"type": "tool_result", "tool_use_id": b.id, "content": "Cancelled: too many tool calls in a row.", "is_error": True}
        for b in final_message.content
        if b.type == "tool_use"
    ]
    messages.append({"role": "assistant", "content": final_message.content})
    messages.append({"role": "user", "content": cancelled})


def _stream(messages: list[dict], on_text: Callable[[str], None], on_status: Callable[[str], None]):
    gen = stream_turn(messages, tools=tool_specs())
    try:
        while True:
            on_text(next(gen))
    except StopIteration as stop:
        final_message = stop.value
        if final_message.stop_reason == "max_tokens":
            # The reply stopped mid-thought because it hit the output cap.
            # Say so out loud: a truncated answer that looks finished is worse
            # than a short one that admits it isn't — especially for the long
            # outputs (web page summaries, note digests) most likely to hit it.
            on_status("that reply hit the length limit and was cut off — ask me to continue it")
            audit.log("truncated_reply", stop_reason="max_tokens")
        return final_message


def _tool_asks_to_confirm(name: str, tool_input: dict) -> bool:
    """Whether a tool wants *this particular call* confirmed.

    Autonomy is per tool, but risk isn't always: "unlock the front door" and
    "turn off the lamp" are the same tool. A tool spanning risk levels supplies
    a predicate over its own arguments.

    A predicate that raises is treated as "confirm". Failing towards asking is
    the only safe direction when the thing that decides has itself broken.
    """
    registered = get_tool(name)
    if registered is None or registered.confirm_when is None:
        return False
    try:
        return bool(registered.confirm_when(tool_input))
    except Exception:
        return True


def _describe_action(name: str, tool_input: dict) -> str:
    """What the user sees before approving an action.

    A tool may supply its own wording. For anything that reaches another
    person, the generic `key=value` dump is not good enough — the recipient
    and the exact text are the whole decision, and they have to be readable
    at a glance rather than picked out of a repr.
    """
    registered = get_tool(name)
    if registered is not None and registered.confirm_description is not None:
        try:
            return registered.confirm_description(tool_input)
        except Exception:
            # A broken description must never swallow the gate itself — fall
            # back to the generic wording rather than skipping confirmation.
            pass

    args = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
    return f"Ada wants to run '{name}' ({args}). This can't be undone automatically."


def _execute_tool_calls(
    final_message,
    on_status: Callable[[str], None],
    confirm: Callable[[str], bool],
) -> list[dict]:
    results = []
    for block in final_message.content:
        if block.type != "tool_use":
            continue

        if safety.is_denied(block.name):
            # Switched off in settings. The model is told plainly, so it can
            # say so rather than silently retrying a tool that will never run.
            on_status(f"blocked: {block.name} is switched off")
            audit.log("tool_denied", tool=block.name, input=block.input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        f"The '{block.name}' tool is switched off in the user's settings and "
                        "cannot run. Tell them plainly; don't try again."
                    ),
                    "is_error": True,
                }
            )
            continue

        try:
            # Ahead of the confirmation prompt on purpose: being over the
            # ceiling should stop the prompt appearing at all, not hand the
            # user one more thing to click through.
            limits.check(block.name)
        except limits.LimitExceeded as e:
            on_status(f"blocked: {block.name} hit its rate limit")
            audit.log("tool_rate_limited", tool=block.name, input=block.input, detail=str(e))
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True}
            )
            continue

        if safety.requires_confirmation(block.name) or _tool_asks_to_confirm(block.name, block.input):
            description = _describe_action(block.name, block.input)
            status.set_pending_confirmation(description)
            try:
                approved = confirm(description)
            finally:
                status.set_pending_confirmation(None)
            audit.log("confirmation_requested", tool=block.name, input=block.input, approved=approved)
            if not approved:
                on_status(f"declined: {block.name}")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "The user declined to confirm this action. It did not run.",
                        "is_error": True,
                    }
                )
                continue

        on_status(f"using {block.name}")
        try:
            output = call_tool(block.name, block.input)
            # Only after it actually ran — a declined or failed action must
            # never count against the user's ceiling.
            limits.record(block.name)
            audit.log("tool_call", tool=block.name, input=block.input, result=output)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        except Exception as e:
            audit.log("tool_call", tool=block.name, input=block.input, error=str(e))
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True})
    return results
