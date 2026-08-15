"""The actual heartbeat checks. Each takes this check's config dict (from
heartbeat_config.json) and decides for itself whether anything is worth
surfacing — most runs should produce nothing.
"""

import time

from ada import store
from ada.heartbeat.notices import add_notice
from ada.heartbeat.state import get_check_state, update_check_state
from ada.tools.reminders import load_reminders, mark_stale_notified

_NOTES_DIR = store.DATA_DIR / "notes"


def check_stale_reminders(params: dict) -> None:
    stale_after_hours = params.get("stale_after_hours", 24)
    now = time.time()

    newly_stale = [
        r
        for r in load_reminders()
        if not r.get("done")
        and not r.get("stale_notified")
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
    "stale_reminders": check_stale_reminders,
    "new_notes": check_new_notes,
}
