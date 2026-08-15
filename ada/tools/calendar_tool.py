"""The user's real macOS calendar — reading it, and putting things on it.

Named `calendar_tool` rather than `calendar` on purpose: the latter shadows
Python's stdlib `calendar` module, which breaks imports in confusing places.

**Why EventKit and not AppleScript.** The obvious no-dependency route is
`osascript` driving Calendar.app, and it was measured before being rejected:
a seven-day event query took **37 seconds** and returned nothing. EventKit
answers the same question in **0.014s** once warm. That's not a preference,
it's the difference between a usable assistant and one you stop asking. The
cost is one macOS-only dependency (`pyobjc-framework-EventKit`) and a Calendar
permission prompt.

**Permission.** Like the messages tool's Full Disk Access, this can't be
granted programmatically. Access is requested lazily on first use — never at
import, so the text REPL doesn't trigger a permission dialog for someone who
never asks about their calendar — and a refusal produces a clear, actionable
message rather than a crash.

**Writes are gated.** `create_event` and `delete_event` are `confirm` in
ada_config.json. A calendar is shared, external state other people may see;
putting something on it is exactly the class of consequential action the
Tier 6 rails exist for.
"""

import threading
import time as _time

from ada import when
from ada.tools.registry import tool

_ACCESS_TIMEOUT_SECONDS = 30

_lock = threading.Lock()
_store = None
_denied_reason: str | None = None

_PERMISSION_HELP = (
    "Ada doesn't have permission to see your calendar. Grant it in "
    "System Settings > Privacy & Security > Calendars, for whichever app you run Ada "
    "from (Terminal, iTerm, etc), then try again."
)


class CalendarUnavailable(Exception):
    """Raised when the calendar can't be reached — no EventKit, or no permission."""


def _event_store():
    """An authorized EKEventStore, or a clear explanation of why not.

    Cached: the permission round-trip costs a few seconds the first time and
    nothing afterwards, and re-asking on every tool call would be rude.
    """
    global _store, _denied_reason

    with _lock:
        if _store is not None:
            return _store
        if _denied_reason is not None:
            raise CalendarUnavailable(_denied_reason)

        try:
            import EventKit
        except ImportError as e:
            _denied_reason = (
                "Calendar support needs the EventKit bindings, which aren't installed "
                "(pip install pyobjc-framework-EventKit). It's macOS-only."
            )
            raise CalendarUnavailable(_denied_reason) from e

        store = EventKit.EKEventStore.alloc().init()
        finished = threading.Event()
        outcome: dict = {}

        def on_result(granted, error):
            outcome["granted"] = bool(granted)
            outcome["error"] = error
            finished.set()

        # requestFullAccessToEventsWithCompletion_ is the macOS 14+ spelling;
        # the older entry point is kept so this doesn't hard-fail on Sonoma-.
        if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(on_result)
        else:
            store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, on_result)

        if not finished.wait(_ACCESS_TIMEOUT_SECONDS):
            # Never block a conversation forever waiting on a dialog nobody is
            # there to answer — the same principle as Tier 6's confirmation gate.
            _denied_reason = "Asking for calendar permission timed out. " + _PERMISSION_HELP
            raise CalendarUnavailable(_denied_reason)

        if not outcome.get("granted"):
            _denied_reason = _PERMISSION_HELP
            raise CalendarUnavailable(_denied_reason)

        _store = store
        return _store


def _epoch_to_nsdate(epoch: float):
    import Foundation

    return Foundation.NSDate.dateWithTimeIntervalSince1970_(epoch)


def _describe(event) -> str:
    start = event.startDate().timeIntervalSince1970()
    calendar_name = event.calendar().title() if event.calendar() else "?"

    if event.isAllDay():
        timing = "all day"
    else:
        end = event.endDate().timeIntervalSince1970() if event.endDate() else None
        timing = when.clock(start) + (f"-{when.clock(end)}" if end else "")

    location = f" @ {event.location()}" if event.location() else ""
    return f"  {timing}  {event.title()}{location}  [{calendar_name}] id={event.eventIdentifier()}"


def _events_between(start_epoch: float, end_epoch: float) -> list:
    store = _event_store()
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        _epoch_to_nsdate(start_epoch), _epoch_to_nsdate(end_epoch), None
    )
    events = list(store.eventsMatchingPredicate_(predicate) or [])
    events.sort(key=lambda e: e.startDate().timeIntervalSince1970())
    return events


def _render(events: list, empty_message: str) -> str:
    if not events:
        return empty_message

    lines = []
    current_day = None
    for event in events:
        day = when.day(event.startDate().timeIntervalSince1970())
        if day != current_day:
            lines.append(f"{day}:")
            current_day = day
        lines.append(_describe(event))
    return "\n".join(lines)


@tool(
    name="list_events",
    description=(
        "Look at what's on the user's real calendar. Use this whenever they ask what "
        "they have on, whether they're free, what's next, or about their schedule for "
        "a day or week. Returns events grouped by day, each with an id you can pass to "
        "delete_event."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "description": "How many days forward to look. Defaults to 7. Use 1 for just today.",
            },
            "start_at": {
                "type": "string",
                "description": (
                    "Optional ISO 8601 start of the window, e.g. '2026-08-20T00:00'. "
                    "Defaults to now. Use this when the user asks about a specific day."
                ),
            },
        },
        "required": [],
    },
)
def list_events(days_ahead: int = 7, start_at: str = "") -> str:
    days_ahead = max(1, min(int(days_ahead), 365))
    start = when.to_epoch(start_at) if start_at else _time.time()
    end = start + days_ahead * 86400

    events = _events_between(start, end)
    window = "today" if days_ahead == 1 else f"the next {days_ahead} days"
    return _render(events, f"Nothing on the calendar for {window}.")


@tool(
    name="find_events",
    description=(
        "Search the user's calendar for events matching a word or phrase — a person's "
        "name, a place, a meeting title. Use this for questions like 'when am I seeing "
        "the dentist' or 'do I have anything with Sarah'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Word or phrase to look for in the title, location or notes."},
            "days_ahead": {
                "type": "integer",
                "description": "How far forward to search, in days. Defaults to 90.",
            },
        },
        "required": ["query"],
    },
)
def find_events(query: str, days_ahead: int = 90) -> str:
    days_ahead = max(1, min(int(days_ahead), 730))
    needle = query.lower()

    def matches(event) -> bool:
        haystack = " ".join(
            str(part) for part in (event.title(), event.location(), event.notes()) if part
        ).lower()
        return needle in haystack

    now = _time.time()
    events = [e for e in _events_between(now, now + days_ahead * 86400) if matches(e)]
    return _render(events, f"Nothing on the calendar matching '{query}' in the next {days_ahead} days.")


@tool(
    name="list_calendars",
    description=(
        "List the user's calendars and which ones can be written to. Use this before "
        "creating an event if the user mentions a specific calendar, or if you need to "
        "ask them which one to use."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_calendars() -> str:
    import EventKit

    store = _event_store()
    calendars = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []
    default = store.defaultCalendarForNewEvents()
    default_title = default.title() if default else None

    lines = []
    for calendar in calendars:
        flags = ["default"] if calendar.title() == default_title else []
        flags.append("writable" if calendar.allowsContentModifications() else "read-only")
        lines.append(f"{calendar.title()} ({', '.join(flags)})")
    return "\n".join(lines) if lines else "No calendars found."


def _pick_calendar(store, calendar_name: str):
    import EventKit

    if not calendar_name:
        default = store.defaultCalendarForNewEvents()
        if default is None:
            raise ValueError("There's no default calendar set — ask the user which calendar to use.")
        return default

    calendars = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []
    for calendar in calendars:
        if calendar.title().lower() == calendar_name.lower():
            if not calendar.allowsContentModifications():
                raise ValueError(f"The '{calendar.title()}' calendar is read-only — pick another.")
            return calendar

    available = ", ".join(c.title() for c in calendars if c.allowsContentModifications())
    raise ValueError(f"No calendar called '{calendar_name}'. Writable ones: {available}.")


@tool(
    name="create_event",
    description=(
        "Put a new event on the user's real calendar. Use this when they ask to schedule, "
        "book, or block out time — 'put lunch with Sam in on Thursday at 1'. Work out the "
        "actual date and time from the current date in your instructions and pass ISO 8601. "
        "If you genuinely don't know how long it should be, ask — but otherwise call this "
        "directly rather than asking them to confirm in conversation first. Running this "
        "tool automatically shows them what's about to be added and waits for a yes or no."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What the event is called."},
            "start_at": {
                "type": "string",
                "description": "When it starts, ISO 8601 local time, e.g. '2026-08-20T13:00'.",
            },
            "end_at": {
                "type": "string",
                "description": "When it ends, ISO 8601. Omit for a one-hour event.",
            },
            "all_day": {"type": "boolean", "description": "True for an all-day event. Defaults to false."},
            "location": {"type": "string", "description": "Optional location."},
            "notes": {"type": "string", "description": "Optional notes/description."},
            "calendar_name": {
                "type": "string",
                "description": "Which calendar to add it to, as shown by list_calendars. Omit for the default.",
            },
        },
        "required": ["title", "start_at"],
    },
)
def create_event(
    title: str,
    start_at: str,
    end_at: str = "",
    all_day: bool = False,
    location: str = "",
    notes: str = "",
    calendar_name: str = "",
) -> str:
    import EventKit

    store = _event_store()
    start = when.to_epoch(start_at)
    end = when.to_epoch(end_at) if end_at else start + 3600

    if end < start:
        raise ValueError("The event would end before it starts — check the times.")

    calendar = _pick_calendar(store, calendar_name)

    event = EventKit.EKEvent.eventWithEventStore_(store)
    event.setTitle_(title)
    event.setStartDate_(_epoch_to_nsdate(start))
    event.setEndDate_(_epoch_to_nsdate(end))
    event.setAllDay_(bool(all_day))
    event.setCalendar_(calendar)
    if location:
        event.setLocation_(location)
    if notes:
        event.setNotes_(notes)

    saved, error = store.saveEvent_span_error_(event, EventKit.EKSpanThisEvent, None)
    if not saved:
        raise ValueError(f"Couldn't save the event: {error}")

    return f"Added '{title}' to {calendar.title()} on {when.stamp(start)}."


@tool(
    name="delete_event",
    description=(
        "Remove an event from the user's real calendar, by the id shown in list_events "
        "or find_events. Call this directly when the user asks to delete or cancel "
        "something — do NOT ask them to confirm in conversation first. Running this "
        "tool automatically shows them exactly what's about to be removed and waits "
        "for a yes or no, so asking beforehand just makes them answer twice."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "The event's id, exactly as shown by list_events."}
        },
        "required": ["event_id"],
    },
)
def delete_event(event_id: str) -> str:
    import EventKit

    store = _event_store()
    event = store.eventWithIdentifier_(event_id)
    if event is None:
        raise ValueError(f"No event with id '{event_id}' — it may already be gone. Try list_events again.")

    title = event.title()
    start = event.startDate().timeIntervalSince1970()

    removed, error = store.removeEvent_span_error_(event, EventKit.EKSpanThisEvent, None)
    if not removed:
        raise ValueError(f"Couldn't remove the event: {error}")

    return f"Removed '{title}' from {when.stamp(start)}."
