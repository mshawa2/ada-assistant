"""The `find_free_time` tool. The arithmetic is in `ada/freetime.py`."""

import time

from ada import freetime, when
from ada.tools import calendar_tool
from ada.tools.registry import tool

MAX_DAYS = 30


def _render_day(day, duration_seconds: float) -> tuple[list[str], list]:
    day_start, window_end = freetime.working_window(day)
    # The events query uses the *whole* working day: a meeting that started at
    # nine and runs to two is still busy at eleven. Only the free-slot window
    # moves forward to now, because a gap that has already passed isn't free,
    # it's over.
    events = calendar_tool._events_between(day_start, window_end)
    window_start = max(day_start, time.time())
    slots = [
        (start, end)
        for start, end in freetime.free_slots(events, window_start, window_end)
        if (end - start) >= duration_seconds
    ]

    lines = []
    if slots:
        lines.append(f"{when.day(window_end)}:")
        for start, end in slots:
            minutes = int((end - start) // 60)
            hours, rest = divmod(minutes, 60)
            length = f"{hours}h{rest:02d}" if hours else f"{rest}m"
            lines.append(f"  {when.clock(start)}–{when.clock(end)}  ({length})")

    all_day = [e for e in events if e.isAllDay()]
    if slots and all_day:
        # Said out loud rather than silently ignored: these were deliberately
        # not treated as busy, and the caller should see what was skipped.
        titles = ", ".join(e.title() for e in all_day)
        lines.append(f"  (all day that day, not counted as busy: {titles})")
    return lines, events


@tool(
    name="find_free_time",
    description=(
        "Find gaps in the user's calendar big enough for something — the answer to "
        "'when am I free', 'when could we meet for an hour', 'do I have time this week', "
        "'find me a slot'. Returns free windows per day within their working hours, "
        "longest-lasting first within each day, and flags any double-bookings it notices. "
        "Use this instead of reading list_events and working the gaps out by hand."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "duration_minutes": {
                "type": "integer",
                "description": "Smallest useful slot, in minutes. Defaults to 30.",
            },
            "days": {
                "type": "integer",
                "description": "How many days forward to look, counting today. Defaults to 7.",
            },
        },
        "required": [],
    },
)
def find_free_time(duration_minutes: int = 30, days: int = 7) -> str:
    duration_minutes = max(5, min(int(duration_minutes), 24 * 60))
    days = max(1, min(int(days), MAX_DAYS))
    duration_seconds = duration_minutes * 60

    blocks, seen_events = [], {}
    for day in freetime.days_ahead(days):
        lines, events = _render_day(day, duration_seconds)
        for event in events:
            # Keyed, not appended. An event running from Monday afternoon to
            # Tuesday morning is returned by *both* days' queries, and two
            # copies of one meeting look exactly like two meetings at once —
            # so the conflict check would report it double-booked against
            # itself. Identity is title-plus-span because the two copies are
            # different objects.
            end = event.endDate()
            seen_events[
                (
                    event.title(),
                    event.startDate().timeIntervalSince1970(),
                    end.timeIntervalSince1970() if end else None,
                )
            ] = event
        if lines:
            blocks.append("\n".join(lines))

    clashes = freetime.conflicts(list(seen_events.values()))
    if clashes:
        # Ahead of the free time: a double-booking you hadn't noticed is worth
        # more than the gap either side of it.
        listed = "\n".join(
            f"  {when.stamp(a.startDate().timeIntervalSince1970())}  {a.title()} / {b.title()}"
            for a, b in clashes
        )
        blocks.insert(0, f"Double-booked:\n{listed}")

    if not blocks:
        return (
            f"No free run of {duration_minutes} minutes in the next {days} "
            f"day{'s' if days != 1 else ''} during working hours."
        )
    return "\n\n".join(blocks)
