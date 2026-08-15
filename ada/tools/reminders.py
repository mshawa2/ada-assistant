import time

from ada import store
from ada.tools.registry import tool

_STORE = store.DATA_DIR / "reminders.json"
_lock = store.lock_for(_STORE)


def _load() -> list[dict]:
    return store.read_json(_STORE, [])


def _save(reminders: list[dict]) -> None:
    store.write_json(_STORE, reminders)


def load_reminders() -> list[dict]:
    """Public, lock-safe read — used by the heartbeat's stale_reminders check."""
    with _lock:
        return _load()


def mark_stale_notified(ids: list[int]) -> None:
    """Used by the heartbeat's stale_reminders check to avoid re-notifying."""
    with _lock:
        reminders = _load()
        for r in reminders:
            if r["id"] in ids:
                r["stale_notified"] = True
        _save(reminders)


@tool(
    name="add_reminder",
    description=(
        "Add a new reminder or task for the user. Use this whenever the user asks "
        "to be reminded of something, or wants to add something to their list."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind the user about."},
            "due": {
                "type": "string",
                "description": "Optional due date/time in plain language (e.g. 'tomorrow', 'Friday'). Omit if not given.",
            },
        },
        "required": ["text"],
    },
)
def add_reminder(text: str, due: str = "") -> str:
    with _lock:
        reminders = _load()
        new_id = max((r["id"] for r in reminders), default=0) + 1
        reminders.append(
            {"id": new_id, "text": text, "due": due, "done": False, "created_at": time.time()}
        )
        _save(reminders)
    return f"Added reminder #{new_id}: {text}" + (f" (due {due})" if due else "")


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
    lines = []
    for r in reminders:
        status = "done" if r["done"] else "pending"
        due = f" (due {r['due']})" if r.get("due") else ""
        lines.append(f"#{r['id']} [{status}] {r['text']}{due}")
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
        for r in reminders:
            if r["id"] == id:
                r["done"] = True
                _save(reminders)
                return f"Marked reminder #{id} as done."
    raise ValueError(f"No reminder with id {id}.")
