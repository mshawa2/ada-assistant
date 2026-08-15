"""Reminders that can actually fire.

`due` stays a human string for display ("tomorrow afternoon"), but what the
heartbeat schedules against is `due_at`: a real epoch timestamp, same units as
`created_at`.

The natural language is resolved by the model, not by a parser here. It knows
today's date and the user's timezone from the system prompt, and it handles
phrasings a hand-rolled parser never would ("the Friday after next", "an hour
before my flight"). What this module does is *validate* what comes back rather
than trust it — a malformed timestamp becomes a clear error the model can see
and correct, never a reminder that silently won't fire.

A reminder with a `due` string but no `due_at` is a real state, not a bug: the
user said something vague enough that no instant was pinned down. It's shown
as explicitly unscheduled rather than quietly never alerting.
"""

import time
from datetime import datetime

from ada import store
from ada.tools.registry import tool

_STORE = store.DATA_DIR / "reminders.json"
_lock = store.lock_for(_STORE)


def _load() -> list[dict]:
    return store.read_json(_STORE, [])


def _save(reminders: list[dict]) -> None:
    store.write_json(_STORE, reminders)


def load_reminders() -> list[dict]:
    """Public, lock-safe read — used by the heartbeat's checks."""
    with _lock:
        return _load()


def _to_epoch(due_at: str) -> float:
    """An ISO 8601 string as epoch seconds. A naive value means local time."""
    try:
        parsed = datetime.fromisoformat(due_at.strip())
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"Couldn't read '{due_at}' as a date and time. "
            "Use ISO 8601 — for example '2026-08-15T15:00' for 3pm on 15 August 2026."
        ) from e
    return parsed.timestamp()


def _describe_due(reminder: dict) -> str:
    due_at = reminder.get("due_at")
    if due_at:
        when = datetime.fromtimestamp(due_at).strftime("%a %d %b at %H:%M")
        overdue = due_at <= time.time() and not reminder.get("done")
        return f" (due {when}{', overdue' if overdue else ''})"
    if reminder.get("due"):
        # Say so plainly: this one will never alert, and the user should know
        # that rather than assuming it's scheduled.
        return f" (due {reminder['due']} — no exact time set, so I can't alert you for it)"
    return ""


def _mark(ids: list[int], field: str) -> None:
    with _lock:
        reminders = _load()
        for reminder in reminders:
            if reminder["id"] in ids:
                reminder[field] = True
        _save(reminders)


def mark_stale_notified(ids: list[int]) -> None:
    """Used by the heartbeat's stale_reminders check to avoid re-notifying."""
    _mark(ids, "stale_notified")


def mark_due_notified(ids: list[int]) -> None:
    """Used by the heartbeat's due_reminders check to avoid re-notifying."""
    _mark(ids, "due_notified")


@tool(
    name="add_reminder",
    description=(
        "Add a new reminder or task for the user. Use this whenever the user asks "
        "to be reminded of something, or wants to add something to their list.\n\n"
        "IMPORTANT: if the user's request implies any time at all — 'tomorrow', "
        "'Friday', 'in an hour', 'at 3', 'tonight' — you must work out the actual "
        "date and time and pass it as due_at. Today's date and the user's timezone "
        "are given to you in your instructions. Without due_at the reminder cannot "
        "alert them; it just sits on a list. If the request genuinely has no time in "
        "it ('remind me to think about the garden'), leave due_at out."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind the user about."},
            "due": {
                "type": "string",
                "description": "How the user phrased the timing, e.g. 'tomorrow afternoon'. For display only.",
            },
            "due_at": {
                "type": "string",
                "description": (
                    "The exact moment to alert them, ISO 8601 in their local time, "
                    "e.g. '2026-08-15T15:00'. This is what actually schedules the alert."
                ),
            },
        },
        "required": ["text"],
    },
)
def add_reminder(text: str, due: str = "", due_at: str = "") -> str:
    due_epoch = _to_epoch(due_at) if due_at else None

    with _lock:
        reminders = _load()
        new_id = max((r["id"] for r in reminders), default=0) + 1
        reminders.append(
            {
                "id": new_id,
                "text": text,
                "due": due,
                "due_at": due_epoch,
                "done": False,
                "created_at": time.time(),
            }
        )
        _save(reminders)

    if due_epoch:
        when = datetime.fromtimestamp(due_epoch).strftime("%a %d %b at %H:%M")
        return f"Added reminder #{new_id}: {text} — I'll tell you on {when}."
    if due:
        return (
            f"Added reminder #{new_id}: {text} (due {due}). "
            "No exact time on it, so it'll sit on the list rather than alerting."
        )
    return f"Added reminder #{new_id}: {text}"


@tool(
    name="list_reminders",
    description=(
        "List the user's reminders/tasks. Use this whenever the user asks what's "
        "on their list, their tasks, or their reminders."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "include_done": {
                "type": "boolean",
                "description": "Whether to include already-completed reminders. Defaults to false.",
            },
        },
        "required": [],
    },
)
def list_reminders(include_done: bool = False) -> str:
    reminders = load_reminders()
    if not include_done:
        reminders = [r for r in reminders if not r["done"]]
    if not reminders:
        return "No reminders."

    # Soonest first, then the undated ones — the order you'd actually want.
    reminders.sort(key=lambda r: (r.get("due_at") is None, r.get("due_at") or 0))

    lines = []
    for reminder in reminders:
        status = "done" if reminder["done"] else "pending"
        lines.append(f"#{reminder['id']} [{status}] {reminder['text']}{_describe_due(reminder)}")
    return "\n".join(lines)


@tool(
    name="complete_reminder",
    description=(
        "Mark a reminder as done, by its id. Use this when the user says they've "
        "finished or completed a task."
    ),
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "The id of the reminder to mark complete."}},
        "required": ["id"],
    },
)
def complete_reminder(id: int) -> str:
    with _lock:
        reminders = _load()
        for reminder in reminders:
            if reminder["id"] == id:
                reminder["done"] = True
                _save(reminders)
                return f"Marked reminder #{id} as done."
    raise ValueError(f"No reminder with id {id}.")


@tool(
    name="snooze_reminder",
    description=(
        "Push a reminder's alert to a later time, by its id. Use this when the user "
        "says things like 'remind me again in an hour', 'not now', or 'push that to "
        "tomorrow'. Work out the new moment and pass it as due_at, the same way you "
        "would for add_reminder."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The id of the reminder to push back."},
            "due_at": {
                "type": "string",
                "description": "The new alert time, ISO 8601 in the user's local time, e.g. '2026-08-15T15:00'.",
            },
        },
        "required": ["id", "due_at"],
    },
)
def snooze_reminder(id: int, due_at: str) -> str:
    due_epoch = _to_epoch(due_at)

    with _lock:
        reminders = _load()
        for reminder in reminders:
            if reminder["id"] == id:
                reminder["due_at"] = due_epoch
                # It's due again, so it's un-notified again — otherwise a snoozed
                # reminder would stay silent forever.
                reminder["due_notified"] = False
                reminder["stale_notified"] = False
                _save(reminders)
                when = datetime.fromtimestamp(due_epoch).strftime("%a %d %b at %H:%M")
                return f"Pushed reminder #{id} to {when}."
    raise ValueError(f"No reminder with id {id}.")
