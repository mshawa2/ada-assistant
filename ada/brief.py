"""One answer to "what do I need to know right now?".

Everything Ada knows about your day already exists, but it exists in five
places: approvals, the calendar, reminders, the heartbeat's notices, the
weather. Asking for all five is five questions, and the answer to four of them
is usually "nothing" — which is exactly the kind of chore that stops getting
done.

**Why this isn't a scheduled prompt.** The obvious route is already available:
`schedules.py` can run "give me a morning briefing" every day at 8am. That
costs a model turn, and it makes the briefing depend on the model remembering
to call five tools in the right order and not getting bored halfway. This costs
nothing, takes milliseconds, and cannot forget a section. Same reasoning as
`rule_options`: if the answer is a lookup and some arithmetic, it should be a
lookup and some arithmetic.

The model still gets the last word — it reads this and says it in its own voice.
What it no longer has to do is *assemble* it.

## Two rules the sections follow

**Quiet when there's nothing.** A section with nothing to report contributes no
lines, and a brief with no sections at all returns the fact plainly rather than
a page of empty headings. Same contract the startup greeting already keeps.

**Loud when it's broken.** A section that raises says so, in place, instead of
being dropped. This matters more than it looks: if the calendar can't be read
because permission was refused, silently omitting it renders as "you have
nothing on today" — a brief confidently telling you your morning is free is
worse than no brief at all. A failure is reported and the other sections still
run, the same bargain `notify.deliver` makes with its channels.
"""

import time

from ada import approvals, settings, when
from ada.heartbeat.notices import list_notices
from ada.tools import calendar_tool, reminders, weather

NOTHING = "Nothing needing you right now: no blocked work, nothing left on the calendar today, no reminders due."

# The check that pushes this every morning (ada/heartbeat/brief_check.py). The
# name lives here rather than there so `_noticed` can exclude it without
# importing the module that imports this one.
PUSH_CHECK = "morning_brief"


def _end_of_today() -> float:
    """Midnight tonight, in the user's configured timezone, as epoch seconds.

    The window is *now* to end-of-day rather than the whole calendar day: a
    brief read at two in the afternoon that leads with the nine o'clock standup
    is reporting history. Overdue reminders are the deliberate exception — those
    stayed your problem when the day moved on.
    """
    midnight = settings.now().replace(hour=23, minute=59, second=59, microsecond=0)
    return midnight.timestamp()


def _blocked() -> list[str]:
    """Jobs stopped waiting on an answer. First, because nothing else in the
    brief is costing you anything by sitting there."""
    waiting = approvals.pending()
    if not waiting:
        return []
    plural = "s" if len(waiting) != 1 else ""
    return [
        f"Waiting on you — {len(waiting)} job{plural} stopped until you answer:",
        *(f"  - Job #{job['id']}: {approvals.describe(job)}" for job in waiting),
    ]


def _calendar() -> list[str]:
    # The private query rather than `list_events`, because the public tool
    # returns text formatted for a different job — grouped by day, with ids for
    # `delete_event` — and re-parsing that back into events to reformat it is
    # the kind of seam that breaks the next time the wording changes.
    events = calendar_tool._events_between(time.time(), _end_of_today())
    if not events:
        return []
    return [
        "Left on the calendar today:",
        *(f"  - {when.clock(e.startDate().timeIntervalSince1970())}  {e.title()}" for e in events),
    ]


def _reminders() -> list[str]:
    """Overdue first, then what's still to come today.

    Undated reminders are counted but not listed. They can't become urgent —
    nothing pins them to today — and listing a standing backlog of twelve
    someday-items under a heading about right now is how a brief becomes
    something you skim past.
    """
    now = time.time()
    end = _end_of_today()
    pending = [r for r in reminders.load_reminders() if not r["done"]]

    overdue = sorted((r for r in pending if r.get("due_at") and r["due_at"] <= now), key=lambda r: r["due_at"])
    later = sorted((r for r in pending if r.get("due_at") and now < r["due_at"] <= end), key=lambda r: r["due_at"])
    undated = sum(1 for r in pending if not r.get("due_at"))

    lines = []
    if overdue:
        lines.append(f"Overdue ({len(overdue)}):")
        lines += [f"  - #{r['id']} {r['text']} — due {when.stamp(r['due_at'])}" for r in overdue]
    if later:
        lines.append("Due later today:")
        lines += [f"  - #{r['id']} {r['text']} — {when.clock(r['due_at'])}" for r in later]
    if undated and lines:
        # Only as a footnote to something that's actually due. On its own it's
        # not news, it's just the size of the list.
        lines.append(f"  ({undated} other reminder{'s' if undated != 1 else ''} with no time set.)")
    return lines


def _noticed() -> list[str]:
    """What the heartbeat saw while you weren't looking.

    Approval notices are dropped: `_blocked` is the authoritative account of
    those, and saying the same sentence twice in one brief reads like a bug —
    the same fix `greeting.startup_summary` already carries.

    This morning's pushed brief is dropped for a sharper reason: it is itself a
    notice, so without this tomorrow's brief would quote today's in full, and
    the day after would quote that.
    """
    unseen = [n for n in list_notices() if n["check"] not in (approvals.NOTICE_CHECK, PUSH_CHECK)]
    if not unseen:
        return []
    return ["Ada noticed:", *(f"  - {n['text']}" for n in unseen)]


def _weather() -> list[str]:
    """Skipped entirely unless `locale.location` is set.

    Guessing the location from the machine's IP is the tempting alternative and
    it's wrong twice over: it sends the user's address to a third party nobody
    asked about, and it's confidently incorrect on a VPN.
    """
    location = (settings.get("locale.location", "") or "").strip()
    if not location:
        return []
    return [f"Weather: {weather.get_weather(location)}"]


# Order is the point: blocked work, then the parts of today that have a clock
# attached, then things merely worth knowing.
SECTIONS = (
    ("blocked work", _blocked),
    ("calendar", _calendar),
    ("reminders", _reminders),
    ("notices", _noticed),
    ("weather", _weather),
)


def compose() -> str:
    """The brief as text. Never raises — a broken section is reported in place."""
    blocks = []
    for name, section in SECTIONS:
        try:
            lines = section()
        except Exception as e:
            # See the module docstring: an omitted section reads as "nothing to
            # report", which for the calendar is an actively misleading claim.
            blocks.append([f"[couldn't read the {name}: {e}]"])
            continue
        if lines:
            blocks.append(lines)

    if not blocks:
        return NOTHING
    return "\n\n".join("\n".join(block) for block in blocks)
