"""The morning push: the day's brief, once, without being asked for it.

`daily_brief` answers "what's on today" when you think to ask. The point of
this is the morning you don't — and, more to the point, the morning you're not
at the Mac at all, which is what the ntfy channel was turned on for.

## Why a check and not a schedule

`schedules.py` can already fire something every morning at eight, and that was
the first design. It fires a *job*, and a job is a model turn: the brief would
cost a few cents a day to assemble something `brief.compose()` produces in
0.12s for nothing, and it would depend on the model remembering to ask for it.

So the schedule machinery is used for the only part worth borrowing — the
arithmetic of "next 08:00 in the user's own timezone", which is fiddly, already
written, and already tested. `next_run` is a pure function on a spec dict, so
this uses it directly and creates no schedule and no job.

## Three things it refuses to do

**Fire on the tick it's turned on.** A first run with no state arms for the
next occurrence instead. Enabling this at three in the afternoon should not
produce a "morning" brief at three in the afternoon.

**Fire late.** If the laptop was shut, the missed run collapses to one — the
same promise `schedules.py` makes, since opening a laptop after a week away to
seven identical briefings is worse than none. But a run that is merely *due*
isn't necessarily still wanted: past `catch_up_hours` the brief is silently
skipped and re-armed for tomorrow, because a push saying "here's your morning"
at ten at night is how someone learns to turn notifications off.

**Say nothing loudly.** An empty brief isn't pushed. That's the same contract
`greeting.startup_summary` keeps, and a daily notification whose usual content
is "nothing" trains you to swipe it away unread, which costs you the morning it
isn't.
"""

import time

from ada import brief, schedules
from ada.heartbeat.notices import add_notice, dismiss_notice, list_notices
from ada.heartbeat.state import get_check_state, update_check_state

DEFAULT_AT = "08:00"
DEFAULT_CATCH_UP_HOURS = 4


def _clear_yesterdays() -> None:
    """Only ever one morning brief outstanding.

    Notices are dismissed by hand, and a brief nobody got round to dismissing
    would otherwise stack up — a week of them, oldest first, in a greeting
    meant to tell you what happened while you were away. Yesterday's brief is
    stale by definition, so posting today's retires it.
    """
    for notice in list_notices():
        if notice["check"] == brief.PUSH_CHECK:
            dismiss_notice(notice["id"])


def check_morning_brief(params: dict) -> None:
    spec = {"repeat": schedules.DAILY, "at": params.get("at", DEFAULT_AT)}
    now = time.time()
    due_at = get_check_state(brief.PUSH_CHECK).get("push_due_at")

    if due_at is None:
        update_check_state(brief.PUSH_CHECK, push_due_at=schedules.next_run(spec, now))
        return
    if now < due_at:
        return

    # Re-armed before anything that can fail, so a brief that throws costs one
    # missed morning rather than a retry on every tick for the rest of the day.
    # Same reasoning as `mark_due_notified` in check_due_reminders.
    update_check_state(brief.PUSH_CHECK, push_due_at=schedules.next_run(spec, now))

    catch_up_seconds = float(params.get("catch_up_hours", DEFAULT_CATCH_UP_HOURS)) * 3600
    if now - due_at > catch_up_seconds:
        return

    text = brief.compose()
    if text == brief.NOTHING and not params.get("push_when_empty", False):
        return

    _clear_yesterdays()
    add_notice(brief.PUSH_CHECK, text, level=params.get("level", "urgent"))
