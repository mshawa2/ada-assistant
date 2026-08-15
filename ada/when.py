"""Turning what the model says about time into instants, and back again.

The model resolves natural language ("tomorrow at 3") into ISO 8601 — it knows
today's date and the user's timezone from the system prompt. This module is
the strict validator on the way in and the formatter on the way out, so a bad
timestamp is a clear error the model can see and correct rather than a silently
mis-scheduled reminder or calendar event.

It lives outside `tools/` because reminders and the calendar both need exactly
this, and two copies of timezone handling is two chances to drift.
"""

from datetime import datetime

from ada import settings


def to_epoch(value: str) -> float:
    """An ISO 8601 string as epoch seconds.

    A naive value is read in the user's *configured* timezone rather than the
    machine's, so an always-on host in another zone still schedules "3pm" at
    3pm where the user actually is.
    """
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"Couldn't read '{value}' as a date and time. "
            "Use ISO 8601 — for example '2026-08-15T15:00' for 3pm on 15 August 2026."
        ) from e

    if parsed.tzinfo is None:
        zone = settings.timezone()
        if zone is not None:
            parsed = parsed.replace(tzinfo=zone)
    return parsed.timestamp()


def local(epoch: float) -> datetime:
    """An epoch timestamp as a datetime in the user's configured timezone."""
    return datetime.fromtimestamp(epoch, settings.timezone())


def stamp(epoch: float) -> str:
    """A short human date+time, honouring the 12h/24h setting."""
    return local(epoch).strftime(f"%a %d %b at {settings.time_format()}")


def day(epoch: float) -> str:
    """Just the date, for grouping a list of events."""
    return local(epoch).strftime("%A %d %B")


def clock(epoch: float) -> str:
    """Just the time of day."""
    return local(epoch).strftime(settings.time_format())
