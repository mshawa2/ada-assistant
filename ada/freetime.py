"""When you're *not* busy — the other half of every scheduling question.

`list_events` answers "what's on". Nobody schedules anything from that: you
schedule from the gaps, and finding gaps by eye across a week of a full
calendar is exactly the tedious, error-prone reading a computer should do. It's
arithmetic over intervals, so like `rule_options` and `brief.py` it costs
nothing and can't be wrong about the maths.

## Three judgement calls, none of them obvious

**All-day events don't make you busy.** "Sarah's birthday" and "Q3 planning
period" are all-day events, and treating them as busy would black out entire
days you're perfectly free on. The cost is that a genuine all-day commitment —
a flight, being out of office — looks free. That's the less damaging error:
suggesting a slot you have to decline beats hiding every slot for a week. They
are still *reported* alongside the day's free time, so the caller sees what it
was ignoring rather than being quietly overruled.

**Only the working window counts.** Free time at three in the morning is not
free time. The window is `locale.day_starts`/`day_ends`, defaulting to
09:00–18:00, and it's a setting because someone else's day starts at six.

**Declined events are still busy.** Ada can't reliably read participation
status through EventKit here, so an event on the calendar is treated as a
commitment. Erring towards busy means the answer is never "you're free" when
you aren't, which is the direction that costs a double-booking.

## Overlaps are a finding, not an error

Two events at once means the calendar already disagrees with itself. That's
worth *saying* rather than silently merging, because a double-booking you
haven't noticed is more useful to hear about than the gap either side of it.
`conflicts` reports them; the free-time maths merges them anyway, since you
can't be more busy than busy.
"""

import time
from datetime import datetime, timedelta

from ada import settings, when

DEFAULT_DAY_STARTS = "09:00"
DEFAULT_DAY_ENDS = "18:00"

# Below this a "free slot" is a gap between two meetings, not time you can use.
MIN_USEFUL_MINUTES = 5


def _clock(setting: str, fallback: str) -> tuple[int, int]:
    raw = str(settings.get(f"locale.{setting}", "") or fallback).strip()
    try:
        hours, minutes = (int(part) for part in raw.split(":"))
    except (ValueError, AttributeError):
        raise ValueError(f"locale.{setting} should be 24-hour HH:MM, like '09:00' — got '{raw}'.") from None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"locale.{setting} isn't a real time of day: '{raw}'.")
    return hours, minutes


def working_window(day: datetime) -> tuple[float, float]:
    """The working day for `day`, as a pair of epoch seconds."""
    start_h, start_m = _clock("day_starts", DEFAULT_DAY_STARTS)
    end_h, end_m = _clock("day_ends", DEFAULT_DAY_ENDS)
    start = day.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = day.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start.timestamp(), end.timestamp()


def busy_intervals(events: list) -> list[tuple[float, float]]:
    """Merged (start, end) pairs for the timed events. All-day ones are skipped
    — see the module docstring."""
    spans = []
    for event in events:
        if event.isAllDay():
            continue
        start = event.startDate().timeIntervalSince1970()
        end_date = event.endDate()
        end = end_date.timeIntervalSince1970() if end_date else start
        if end > start:
            spans.append((start, end))

    spans.sort()
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def conflicts(events: list) -> list[tuple]:
    """Pairs of timed events that overlap. The calendar disagreeing with itself
    is a finding worth surfacing, not a case to quietly merge away."""
    timed = sorted(
        (e for e in events if not e.isAllDay() and e.endDate()),
        key=lambda e: e.startDate().timeIntervalSince1970(),
    )
    found = []
    for i, earlier in enumerate(timed):
        earlier_end = earlier.endDate().timeIntervalSince1970()
        for later in timed[i + 1 :]:
            if later.startDate().timeIntervalSince1970() >= earlier_end:
                break  # sorted by start, so nothing after this one overlaps either
            found.append((earlier, later))
    return found


def free_slots(events: list, window_start: float, window_end: float) -> list[tuple[float, float]]:
    """The gaps inside one window that no timed event covers."""
    if window_end <= window_start:
        return []

    slots, cursor = [], window_start
    for busy_start, busy_end in busy_intervals(events):
        if busy_end <= window_start or busy_start >= window_end:
            continue
        if busy_start > cursor:
            slots.append((cursor, min(busy_start, window_end)))
        cursor = max(cursor, busy_end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        slots.append((cursor, window_end))

    return [(s, e) for s, e in slots if (e - s) >= MIN_USEFUL_MINUTES * 60]


def days_ahead(count: int, from_epoch: float | None = None) -> list:
    """`count` local dates starting today, as datetimes at midnight."""
    start = when.local(from_epoch if from_epoch is not None else time.time())
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return [midnight + timedelta(days=offset) for offset in range(count)]
