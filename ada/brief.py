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

from ada import approvals, extensions, location, notify, settings, when
from ada.heartbeat.notices import list_notices
from ada.tools import calendar_tool, email_tool, reminders, weather

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


def _alerting() -> list[str]:
    """Notification channels that are configured but can't send.

    First in the brief, ahead even of blocked work, because it is the one item
    that bears on whether you are seeing any of the rest. A phone push that was
    never really configured looks exactly like one that works until the morning
    it mattered, and `deliver` can only say so into a log — which on the launchd
    daemon is a file nobody reads.
    """
    broken = notify.unavailable_channels()
    if not broken:
        return []
    return ["Alerting is degraded:", *(f"  - {channel}: {why}" for channel, why in broken)]


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


def _email() -> list[str]:
    """How much unread mail is waiting.

    Silent when there's no account configured, rather than printing the setup
    guide every morning — the same bargain `_weather` makes about an unset
    location. Silent at zero too, because "0 unread" is not news and a brief
    that reports every absence stops being read.

    A count, not a list. The brief's job is to tell you whether the inbox needs
    you at all; deciding *what* to answer is `list_email`'s, and a morning brief
    that inlines ten subject lines has become the inbox it was summarizing.
    """
    if not email_tool.is_configured():
        return []
    count = email_tool.unread_count()
    if not count:
        return []
    return [f"Inbox: {count} unread email{'s' if count != 1 else ''}."]


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


def _device_weather() -> str:
    """The forecast for wherever this Mac currently is.

    A coordinate pair goes straight to the forecast; anything else is treated as
    a place name and geocoded. See `ada/location.py` for why the shortcut exists
    rather than CoreLocation.
    """
    answer = location.here()
    point = location.looks_like_coordinates(answer)
    if point:
        return weather.report_at(point[0], point[1], "Where you are")

    # A postal address needs its locality dug out; see place_candidates. The
    # first one the geocoder recognises wins, and the forecast names the place
    # it resolved, so a wrong guess shows up in the brief instead of hiding.
    for candidate in location.place_candidates(answer):
        try:
            return weather.get_weather(candidate)
        except ValueError:
            continue
    raise location.LocationUnavailable(
        f"'{location.shortcut_name()}' returned something the weather service didn't recognise as a place"
    )


def _weather() -> list[str]:
    """Device location if `locale.location_source` is "device", else the fixed
    `locale.location`. Nothing at all if neither is configured.

    Guessing from the machine's IP remains the tempting third option and is
    still wrong twice over: it sends the user's address to a third party nobody
    asked about, and it reports the VPN exit node rather than the user.

    A device lookup that fails falls back to the fixed location when there is
    one — travel with the Mac, and a denied permission still tells you the
    weather at home rather than nothing. The fallback says it is one, because
    "it's fine in Austin" is actively misleading when you're in Berlin.
    """
    fixed = (settings.get("locale.location", "") or "").strip()

    if (settings.get("locale.location_source", "config") or "config").strip() == "device":
        try:
            return [f"Weather: {_device_weather()}"]
        except location.LocationUnavailable as e:
            if not fixed:
                return [f"Weather: couldn't read this Mac's location — {e}"]
            return [
                f"Weather: couldn't read this Mac's location, so this is {fixed}, not necessarily where you are — {e}",
                f"  {weather.get_weather(fixed)}",
            ]

    if not fixed:
        return []
    return [f"Weather: {weather.get_weather(fixed)}"]


# Order is the point: whether you can be reached at all, then blocked work,
# then the parts of today that have a clock attached, then what's waiting on
# you, then things merely worth knowing.
SECTIONS = (
    ("notification channels", _alerting),
    ("blocked work", _blocked),
    ("calendar", _calendar),
    ("reminders", _reminders),
    ("email", _email),
    ("notices", _noticed),
    ("weather", _weather),
)


def compose() -> str:
    """The brief as text. Never raises — a broken section is reported in place."""
    blocks = []
    extensions.load()
    for name, section in (*SECTIONS, *extensions.brief_sections):
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
