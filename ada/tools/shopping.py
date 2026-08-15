"""The shopping list — kept in Reminders, not here.

A shopping list is useless where you aren't. Storing it in `data/` like notes
or memory would put it on the laptop, and the moment you actually need it
you're standing in a supermarket holding a phone. So it lives in macOS
Reminders, which syncs to that phone by itself. EventKit is already a
dependency for the calendar; reminders are a separate permission on the same
framework.

This is not `reminders.py`. That one is time-based tasks that fire at a
moment — "call the dentist at 3pm". A shopping list is a standing set of
items with no due date, ticked off in a shop. Same word, different thing, and
merging them would make both worse.

One tool with an action, rather than four. Adding two items and listing the
result are one intent each, and a model picks an action from an enum more
reliably than it picks between `add_shopping_item` and `list_shopping_items`.
`clear` is the only destructive one, so it's the only one that asks — via
`confirm_when`, the same per-call mechanism that tells a lock from a lamp.
"""

import threading

from ada import settings
from ada.tools.registry import tool

_ACCESS_TIMEOUT_SECONDS = 30
MAX_ITEMS_SHOWN = 60

ACTIONS = ("list", "add", "remove", "clear")

_lock = threading.Lock()
_store = None
_denied_reason: str | None = None

_PERMISSION_HELP = (
    "Ada doesn't have permission to see your reminders. Grant it in "
    "System Settings > Privacy & Security > Reminders, for whichever app you run Ada "
    "from (Terminal, iTerm, etc), then try again."
)


class ShoppingUnavailable(Exception):
    """Raised when Reminders can't be reached, or the list doesn't exist."""


def _reminder_store():
    """An EKEventStore authorized for *reminders*, or a clear explanation.

    Separate from the calendar's store on purpose: reminders are their own
    permission, and someone who only ever asks about their calendar shouldn't
    be prompted for access to their reminders.
    """
    global _store, _denied_reason

    with _lock:
        if _store is not None:
            return _store
        if _denied_reason is not None:
            raise ShoppingUnavailable(_denied_reason)

        try:
            import EventKit
        except ImportError as e:
            _denied_reason = (
                "Shopping lists need the EventKit bindings, which aren't installed "
                "(pip install pyobjc-framework-EventKit). It's macOS-only."
            )
            raise ShoppingUnavailable(_denied_reason) from e

        store = EventKit.EKEventStore.alloc().init()
        finished = threading.Event()
        outcome: dict = {}

        def on_result(granted, _error):
            outcome["granted"] = bool(granted)
            finished.set()

        if hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
            store.requestFullAccessToRemindersWithCompletion_(on_result)
        else:
            store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeReminder, on_result)

        if not finished.wait(_ACCESS_TIMEOUT_SECONDS):
            _denied_reason = "Asking for reminders permission timed out. " + _PERMISSION_HELP
            raise ShoppingUnavailable(_denied_reason)

        if not outcome.get("granted"):
            _denied_reason = _PERMISSION_HELP
            raise ShoppingUnavailable(_denied_reason)

        _store = store
        return _store


def _list_name() -> str:
    return (settings.get("shopping.list_name") or "Shopping").strip()


def _the_list():
    """The configured Reminders list, or an error naming what does exist.

    Deliberately doesn't create it, and deliberately doesn't fall back to the
    default list: silently scattering shopping items through someone's general
    reminders is worse than one clear message asking them to pick.
    """
    import EventKit

    store = _reminder_store()
    wanted = _list_name().lower()
    available = list(store.calendarsForEntityType_(EventKit.EKEntityTypeReminder) or [])

    for calendar in available:
        if calendar.title().lower() == wanted:
            if not calendar.allowsContentModifications():
                raise ShoppingUnavailable(f"The '{calendar.title()}' list is read-only.")
            return store, calendar

    names = ", ".join(c.title() for c in available) or "none"
    raise ShoppingUnavailable(
        f"There's no Reminders list called '{_list_name()}'. Either make one in "
        f"Reminders.app, or point Ada at an existing one by setting "
        f"'shopping.list_name' — the lists that exist are: {names}."
    )


def _fetch_items(store, calendar) -> list:
    """Outstanding items on the list. Blocks on EventKit's async fetch."""
    # Exact spelling matters and was got wrong once: it's ...RemindersWith...,
    # not ...RemindersIn.... A faked store happily answers to whatever name the
    # code invents, so only a real call catches this.
    predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
        None, None, [calendar]
    )
    finished = threading.Event()
    found: list = []

    def on_result(reminders):
        found.extend(reminders or [])
        finished.set()

    store.fetchRemindersMatchingPredicate_completion_(predicate, on_result)
    if not finished.wait(_ACCESS_TIMEOUT_SECONDS):
        raise ShoppingUnavailable("Reminders took too long to answer.")
    return found


def _render(items: list) -> str:
    if not items:
        return f"The {_list_name()} list is empty."

    shown = items[:MAX_ITEMS_SHOWN]
    lines = [f"{_list_name()} ({len(items)} item{'s' if len(items) != 1 else ''}):"]
    lines += [f"  {item.title()}" for item in shown]
    if len(items) > len(shown):
        lines.append(f"  ({len(items) - len(shown)} more.)")
    return "\n".join(lines)


def _as_items(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in (value or []) if str(v).strip()]


@tool(
    name="shopping_list",
    description=(
        "Read or change the user's shopping list, which lives in their Reminders app so "
        "it's on their phone in the shop.\n\n"
        "action='list' to read it, 'add' with items to put things on, 'remove' with items "
        "to tick things off, and 'clear' to empty it (which asks first). Pass every item "
        "in one call — 'add milk and bread' is one call with two items, not two calls.\n\n"
        "This is separate from add_reminder: use that for things that happen at a time "
        "('call the dentist at 3'), and this for things to buy."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "What to do. Defaults to 'list'.",
            },
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The items, for 'add' and 'remove'. e.g. ['milk', 'bread'].",
            },
        },
        "required": [],
    },
    confirm_when=lambda tool_input: (tool_input.get("action") or "").strip().lower() == "clear",
    confirm_description=lambda _tool_input: (
        f"Ada wants to CLEAR the whole {_list_name()} list.\n"
        "    Everything currently on it will be ticked off."
    ),
)
def shopping_list(action: str = "list", items=None) -> str:
    import EventKit

    action = (action or "list").strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"'{action}' isn't a shopping list action. Use one of: {', '.join(ACTIONS)}.")

    store, calendar = _the_list()

    if action == "list":
        return _render(_fetch_items(store, calendar))

    if action == "add":
        wanted = _as_items(items)
        if not wanted:
            raise ValueError("Nothing to add — say what to put on the list.")

        existing = {r.title().strip().lower() for r in _fetch_items(store, calendar)}
        added, already = [], []
        for name in wanted:
            if name.lower() in existing:
                already.append(name)
                continue
            reminder = EventKit.EKReminder.reminderWithEventStore_(store)
            reminder.setTitle_(name)
            reminder.setCalendar_(calendar)
            saved, error = store.saveReminder_commit_error_(reminder, True, None)
            if not saved:
                raise ValueError(f"Couldn't add '{name}': {error}")
            added.append(name)
            existing.add(name.lower())

        parts = []
        if added:
            parts.append(f"Added {', '.join(added)} to {_list_name()}.")
        if already:
            parts.append(f"Already on it: {', '.join(already)}.")
        return " ".join(parts)

    current = _fetch_items(store, calendar)

    if action == "remove":
        wanted = _as_items(items)
        if not wanted:
            raise ValueError("Nothing to remove — say what to take off the list.")

        removed, missing = [], []
        for name in wanted:
            match = next((r for r in current if name.lower() in r.title().strip().lower()), None)
            if match is None:
                missing.append(name)
                continue
            # Completed rather than deleted: ticking something off is what a
            # shopping list does, and it stays recoverable in Reminders.
            match.setCompleted_(True)
            saved, error = store.saveReminder_commit_error_(match, True, None)
            if not saved:
                raise ValueError(f"Couldn't tick off '{name}': {error}")
            removed.append(match.title())
            current.remove(match)

        parts = []
        if removed:
            parts.append(f"Ticked off {', '.join(removed)}.")
        if missing:
            parts.append(f"Not on the list: {', '.join(missing)}.")
        return " ".join(parts)

    # clear
    if not current:
        return f"The {_list_name()} list is already empty."
    for reminder in current:
        reminder.setCompleted_(True)
        store.saveReminder_commit_error_(reminder, True, None)
    return f"Cleared {len(current)} item{'s' if len(current) != 1 else ''} from {_list_name()}."
