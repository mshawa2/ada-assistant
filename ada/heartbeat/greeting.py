"""Builds the 'while you were away' summary shown at the start of a session.
Quiet by default: returns None (say nothing) unless there's genuinely
something to report.

Blocked work leads. A notice is something Ada observed; a pending approval is
something *stopped* until the user answers, and it is checked independently of
the notice log — dismissing the notice mustn't make a job waiting on you
invisible. Quiet hours don't suppress it either: by the time you're reading a
greeting you're already here, and holding back the one thing that needs you is
the wrong kind of quiet.
"""

from ada import approvals
from ada.heartbeat.config import load_config
from ada.heartbeat.notices import list_notices
from ada.heartbeat.quiet_hours import in_quiet_hours


def _waiting_on_you() -> str | None:
    waiting = approvals.pending()
    if not waiting:
        return None

    lines = "\n".join(f"- Job #{j['id']}: {approvals.describe(j)}" for j in waiting)
    plural = "s" if len(waiting) != 1 else ""
    return f"{len(waiting)} job{plural} stopped waiting for you to approve something:\n{lines}"


def startup_summary() -> str | None:
    blocked = _waiting_on_you()
    # Approval notices are dropped from the observation list: the blocked
    # section above is the authoritative account of them, and saying the same
    # sentence twice in one greeting reads like a bug, which it was.
    notices = [n for n in list_notices() if n["check"] != approvals.NOTICE_CHECK]
    if not notices:
        return blocked

    quiet = in_quiet_hours(load_config().get("quiet_hours", {}))

    interrupt_worthy = [n for n in notices if n["level"] in ("urgent", "critical")]
    calm = [n for n in notices if n["level"] == "normal"]

    shown = [n for n in interrupt_worthy if n["level"] == "critical" or not quiet]
    held_count = len(interrupt_worthy) - len(shown)

    parts = [blocked] if blocked else []
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
