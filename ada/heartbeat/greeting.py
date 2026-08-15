"""Builds the 'while you were away' summary shown at the start of a session.
Quiet by default: returns None (say nothing) unless there's genuinely
something to report.
"""

from ada.heartbeat.config import load_config
from ada.heartbeat.notices import list_notices
from ada.heartbeat.quiet_hours import in_quiet_hours


def startup_summary() -> str | None:
    notices = list_notices()
    if not notices:
        return None

    quiet = in_quiet_hours(load_config().get("quiet_hours", {}))

    interrupt_worthy = [n for n in notices if n["level"] in ("urgent", "critical")]
    calm = [n for n in notices if n["level"] == "normal"]

    shown = [n for n in interrupt_worthy if n["level"] == "critical" or not quiet]
    held_count = len(interrupt_worthy) - len(shown)

    parts = []
    if shown:
        lines = "\n".join(f"- {n['text']}" for n in shown)
        parts.append(f"While you were away, I noticed:\n{lines}")
    if held_count:
        plural = "s" if held_count != 1 else ""
        parts.append(f"({held_count} more noteworthy thing{plural} held for quiet hours.)")
    if calm:
        plural = "s" if len(calm) != 1 else ""
        parts.append(f"Also {len(calm)} quieter note{plural} logged — just ask what I've noticed.")

    return "\n".join(parts) if parts else None
