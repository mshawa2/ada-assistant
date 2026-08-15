"""The actual heartbeat checks. Each takes this check's config dict (from
heartbeat_config.json) and decides for itself whether anything is worth
surfacing — most runs should produce nothing.
"""

import time

from ada import store
from ada.heartbeat.notices import add_notice
from ada.heartbeat.state import get_check_state, update_check_state
from ada.tools.reminders import load_reminders, mark_due_notified, mark_stale_notified

_NOTES_DIR = store.DATA_DIR / "notes"


def check_due_reminders(params: dict) -> None:
    """Fire reminders that have reached their time.

    This is what makes a reminder a reminder rather than a list entry. Only
    reminders with a real `due_at` can be here — a vague one was never
    scheduled, and `stale_reminders` is what eventually catches those.

    Anything already overdue when the process starts fires on the catch-up
    pass, so closing the laptop delays an alert rather than losing it.
    """
    lead_minutes = params.get("lead_minutes", 0)
    level = params.get("level", "urgent")
    cutoff = time.time() + lead_minutes * 60

    now_due = [
        r
        for r in load_reminders()
        if not r.get("done") and not r.get("due_notified") and r.get("due_at") and r["due_at"] <= cutoff
    ]
    if not now_due:
        return

    # Marked before the notice is raised: if adding the notice throws, the
    # worst case is one missed alert rather than an alert every single tick.
    mark_due_notified([r["id"] for r in now_due])

    for reminder in now_due:
        add_notice("due_reminders", f'Reminder: {reminder["text"]}', level=level)


def check_stale_reminders(params: dict) -> None:
    """Nag about undated reminders that have been sitting untouched.

    Scoped to reminders with no `due_at`: one scheduled for next month isn't
    stale, it's just not due yet, and `due_reminders` already owns that case.
    """
    stale_after_hours = params.get("stale_after_hours", 24)
    now = time.time()

    newly_stale = [
        r
        for r in load_reminders()
        if not r.get("done")
        and not r.get("stale_notified")
        and not r.get("due_at")
        and r.get("created_at") is not None
        and (now - r["created_at"]) / 3600 >= stale_after_hours
    ]
    if not newly_stale:
        return

    mark_stale_notified([r["id"] for r in newly_stale])
    items = "; ".join(f'"{r["text"]}"' for r in newly_stale)
    plural = len(newly_stale) > 1
    add_notice(
        "stale_reminders",
        f"{len(newly_stale)} reminder{'s' if plural else ''} "
        f"{'have' if plural else 'has'} been sitting untouched for a while: {items}.",
        level="urgent",
    )


def check_new_notes(params: dict) -> None:
    _NOTES_DIR.mkdir(parents=True, exist_ok=True)
    current_files = sorted(p.name for p in _NOTES_DIR.iterdir() if p.is_file())

    check_state = get_check_state("new_notes")
    if "seen_files" not in check_state:
        # First run ever: nothing was actually "new" before we started watching.
        update_check_state("new_notes", seen_files=current_files)
        return

    new_files = [f for f in current_files if f not in set(check_state["seen_files"])]
    if new_files:
        add_notice(
            "new_notes",
            f"Noticed {len(new_files)} new note{'s' if len(new_files) > 1 else ''}: {', '.join(new_files)}.",
            level="normal",
        )
    update_check_state("new_notes", seen_files=current_files)


CHECKS = {
    "due_reminders": check_due_reminders,
    "stale_reminders": check_stale_reminders,
    "new_notes": check_new_notes,
}
