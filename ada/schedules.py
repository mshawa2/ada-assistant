"""Recurring work: the crontab, not the process.

`run_at` on a job already says "not before three". What it can't say is "every
morning at eight", and there is nowhere to put that — a job is one piece of
work with one outcome, and a thing that happens every day is not one piece of
work.

So a schedule is a **template**, and firing it **creates a job**. That's the
distinction between a crontab entry and the processes it spawns, and keeping
them apart is what makes "what did the 8am briefing say on Tuesday?" a
question with an answer: each run is its own record with its own result and its
own history. Re-arming a single job would overwrite yesterday's answer with
today's, and `done` is terminal for good reasons besides.

It also means a schedule inherits everything a job already has, for free —
claiming, parking for approval, the tool allowlist, the budget slice, orphan
recovery. This module adds no new way to run anything. It only decides *when*.

## The vocabulary is deliberately small

`hourly`, `daily`, `weekly`, `monthly`, and a raw `every_seconds`. No cron
expressions: that means a parser or a dependency, for a single user who wants
a morning briefing and a Sunday review. This is the same trade `routing.py`
makes with lexical scoring over embeddings — the small thing does the real job,
and what it can't express is written down rather than half-supported.

What it can't express: "every weekday", "the last Friday of the month", "every
15 minutes past the hour". `every_seconds` covers the third crudely. The first
two would need a real cron parser, and that's the moment to add one.

## Times are local and computed forward

Next-run is always computed **strictly after** a reference instant, in the
user's configured timezone rather than the machine's, so an always-on host in
another zone still fires "08:00" at eight where the user actually is. A run
that's missed while the laptop is shut fires once on the next tick and then
resumes its normal rhythm — it doesn't fire once per missed occurrence, which
would mean opening a laptop after a week away to seven identical briefings.
"""

import time
from datetime import datetime, timedelta

from ada import store, when

_STORE = store.DATA_DIR / "schedules.json"
_lock = store.lock_for(_STORE)

HOURLY = "hourly"
DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"
EVERY_SECONDS = "every_seconds"
REPEATS = (HOURLY, DAILY, WEEKLY, MONTHLY, EVERY_SECONDS)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# A monthly schedule can't ask for the 30th: February doesn't have one, and
# "the nearest real day" is a guess about what someone meant. 28 is the last
# day every month actually has.
MAX_MONTH_DAY = 28

# Below a minute this stops being a schedule and starts being a loop, and
# every tick of it costs a model call.
MIN_SECONDS = 60


# --- the spec ---------------------------------------------------------------


def _clock(spec: dict) -> tuple[int, int]:
    at = str(spec.get("at") or "09:00").strip()
    try:
        hours, minutes = at.split(":")
        hours, minutes = int(hours), int(minutes)
    except (ValueError, AttributeError) as e:
        raise ValueError(f"Couldn't read '{at}' as a time of day. Use 24-hour HH:MM, like '08:30'.") from e
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"'{at}' isn't a real time of day. Use 24-hour HH:MM, like '08:30'.")
    return hours, minutes


def validate(spec: dict) -> dict:
    """Check a schedule spec, or raise something the model can act on."""
    if not isinstance(spec, dict):
        raise ValueError("A schedule needs to say how often to repeat.")

    repeat = str(spec.get("repeat") or "").strip().lower()
    if repeat not in REPEATS:
        raise ValueError(
            f"'{spec.get('repeat')}' isn't a repeat I understand. Use one of: {', '.join(REPEATS)}."
        )

    checked = {"repeat": repeat}

    if repeat == EVERY_SECONDS:
        try:
            seconds = int(spec.get("every_seconds") or spec.get("seconds") or 0)
        except (TypeError, ValueError) as e:
            raise ValueError("every_seconds needs a whole number of seconds.") from e
        if seconds < MIN_SECONDS:
            raise ValueError(
                f"{seconds} seconds is too often — the minimum is {MIN_SECONDS}. "
                "Anything faster is a loop, and each run costs a model call."
            )
        checked["every_seconds"] = seconds
        return checked

    if repeat != HOURLY:
        checked["at"] = "{:02d}:{:02d}".format(*_clock(spec))

    if repeat == WEEKLY:
        day = str(spec.get("on") or "").strip().lower()
        if day not in DAYS:
            raise ValueError(f"'{spec.get('on')}' isn't a day of the week. Use one of: {', '.join(DAYS)}.")
        checked["on"] = day

    if repeat == MONTHLY:
        try:
            day = int(spec.get("on") or 1)
        except (TypeError, ValueError) as e:
            raise ValueError("A monthly schedule needs a day of the month, 1 to 28.") from e
        if not 1 <= day <= MAX_MONTH_DAY:
            raise ValueError(
                f"{day} isn't a day every month has — use 1 to {MAX_MONTH_DAY}. "
                "February is why."
            )
        checked["on"] = day

    return checked


def describe(spec: dict) -> str:
    """The schedule as a person would say it."""
    repeat = spec.get("repeat")
    if repeat == EVERY_SECONDS:
        seconds = spec.get("every_seconds", 0)
        if seconds % 3600 == 0:
            hours = seconds // 3600
            return f"every {hours} hours" if hours > 1 else "every hour"
        minutes = max(1, seconds // 60)
        return f"every {minutes} minutes" if minutes > 1 else "every minute"
    if repeat == HOURLY:
        return "every hour"
    if repeat == DAILY:
        return f"every day at {spec.get('at')}"
    if repeat == WEEKLY:
        return f"every {spec.get('on', '').title()} at {spec.get('at')}"
    if repeat == MONTHLY:
        return f"on the {spec.get('on')}{_ordinal(spec.get('on', 1))} of each month at {spec.get('at')}"
    return "on no schedule at all"


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


# --- when it next runs ------------------------------------------------------


def next_run(spec: dict, after: float | None = None) -> float:
    """The next instant this schedule fires, strictly after `after`.

    Strictly, so firing a schedule and then asking for its next run can't hand
    back the moment that just passed and fire it again on the same tick.
    """
    after = time.time() if after is None else after
    repeat = spec.get("repeat")

    if repeat == EVERY_SECONDS:
        return after + spec["every_seconds"]
    if repeat == HOURLY:
        return after + 3600

    hours, minutes = _clock(spec)
    local = when.local(after)
    candidate = local.replace(hour=hours, minute=minutes, second=0, microsecond=0)

    if repeat == DAILY:
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    if repeat == WEEKLY:
        wanted = DAYS.index(spec["on"])
        ahead = (wanted - candidate.weekday()) % 7
        candidate += timedelta(days=ahead)
        if candidate <= local:
            candidate += timedelta(days=7)
        return candidate.timestamp()

    if repeat == MONTHLY:
        candidate = candidate.replace(day=spec["on"])
        if candidate <= local:
            candidate = _add_month(candidate)
        return candidate.timestamp()

    raise ValueError(f"'{repeat}' isn't a repeat I understand.")


def _add_month(moment: datetime) -> datetime:
    """The same day next month. Safe because days are capped at 28."""
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1)
    return moment.replace(month=moment.month + 1)


# --- the store --------------------------------------------------------------


def _load() -> list[dict]:
    return store.read_json(_STORE, [])


def _save(schedules: list[dict]) -> None:
    store.write_json(_STORE, schedules)


def load_schedules() -> list[dict]:
    with _lock:
        return _load()


def get(schedule_id: int) -> dict | None:
    with _lock:
        return next((s for s in _load() if s["id"] == schedule_id), None)


def require(schedule_id: int) -> dict:
    schedule = get(schedule_id)
    if schedule is None:
        raise ValueError(f"No schedule with id {schedule_id}.")
    return schedule


def create(
    task: str,
    spec: dict,
    tools: list[str] | None = None,
    budget_usd: float | None = None,
) -> dict:
    """Add a recurring entry. Validated now, so a bad one is a clear error the
    model can correct rather than something that silently never fires."""
    task = (task or "").strip()
    if not task:
        raise ValueError("A schedule needs a task — say what should actually be done each time.")

    checked = validate(spec)

    with _lock:
        schedules = _load()
        schedule = {
            "id": max((s["id"] for s in schedules), default=0) + 1,
            "task": task,
            "spec": checked,
            "tools": tools,
            "budget_usd": budget_usd,
            "enabled": True,
            "created_at": time.time(),
            "next_due_at": next_run(checked),
            "last_run_at": None,
            "last_job_id": None,
            "runs": 0,
        }
        schedules.append(schedule)
        _save(schedules)
        return schedule


def due(now: float | None = None) -> list[dict]:
    """Enabled entries whose time has come, soonest first."""
    now = time.time() if now is None else now
    ready = [s for s in load_schedules() if s.get("enabled", True) and s.get("next_due_at", 0) <= now]
    ready.sort(key=lambda s: (s.get("next_due_at", 0), s["id"]))
    return ready


def mark_fired(schedule_id: int, job_id: int, now: float | None = None) -> dict:
    """Record that this entry produced a job, and arm it for next time.

    `next_run` is computed from *now* rather than from the missed due time, so
    a laptop opened after a week away fires once and resumes its rhythm instead
    of catching up on seven identical briefings.
    """
    now = time.time() if now is None else now
    with _lock:
        schedules = _load()
        schedule = next((s for s in schedules if s["id"] == schedule_id), None)
        if schedule is None:
            raise ValueError(f"No schedule with id {schedule_id}.")

        schedule["last_run_at"] = now
        schedule["last_job_id"] = job_id
        schedule["runs"] = schedule.get("runs", 0) + 1
        schedule["next_due_at"] = next_run(schedule["spec"], after=now)
        _save(schedules)
        return schedule


def set_enabled(schedule_id: int, enabled: bool) -> dict:
    with _lock:
        schedules = _load()
        schedule = next((s for s in schedules if s["id"] == schedule_id), None)
        if schedule is None:
            raise ValueError(f"No schedule with id {schedule_id}.")
        schedule["enabled"] = bool(enabled)
        if enabled:
            # Re-arm from now, not from whenever it was paused — otherwise
            # resuming a schedule paused last month fires it immediately.
            schedule["next_due_at"] = next_run(schedule["spec"])
        _save(schedules)
        return schedule


def delete(schedule_id: int) -> dict:
    with _lock:
        schedules = _load()
        schedule = next((s for s in schedules if s["id"] == schedule_id), None)
        if schedule is None:
            raise ValueError(f"No schedule with id {schedule_id}.")
        _save([s for s in schedules if s["id"] != schedule_id])
        return schedule


def summarize(schedule: dict) -> str:
    """One entry as a line a person reads."""
    line = f"#{schedule['id']} {schedule['task']} — {describe(schedule['spec'])}"
    if not schedule.get("enabled", True):
        return line + " (paused)"
    line += f", next {when.stamp(schedule['next_due_at'])}"
    if schedule.get("runs"):
        line += f" (run {schedule['runs']}x)"
    return line
