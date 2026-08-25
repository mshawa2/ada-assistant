"""The face's HTTP surface.

Was a read-only dashboard. It's now what the interface talks to, which means
one new thing that matters: `/api/confirm` lets a click answer a confirmation
the terminal is also waiting on. See `ada/confirm.py` for why that's a race
rather than a handover.

Everything here is local-only and unauthenticated, which is fine for a server
bound to 127.0.0.1 — but it's the reason `/api/confirm` takes the
confirmation's id. A stale tab clicking "approve" after the question changed
would otherwise approve the *new* question, which is exactly the sort of thing
a gate exists to prevent.

**Two kinds of state, and it matters which is which.** `status.py` is process
memory: the live conversation, what's armed, a confirmation being waited on
right now. Everything else — jobs, approvals, schedules, notices, spend — is on
disk under `data/`. That split is what decides which panels still work when the
face is served by the always-on daemon rather than by a REPL: the durable half
does, the live half doesn't, and `/api/conversation` exists so the chat panel
falls back to the persisted transcript instead of looking dead.

`/api/approvals/<id>/<decision>` is the one that earns the dashboard its keep.
With the daemon running and no terminal open, a job that parks for approval had
no way to be answered at all — you had to start a REPL to unblock it.
"""

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from ada import approvals, audit, budget, cost, jobs, positions, recap, schedules, session, status
from ada.heartbeat.kill_switch import is_paused, set_paused
from ada.heartbeat.notices import dismiss_notice, list_notices
from ada.tools import specs as tool_specs

_STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(_STATIC_DIR, "index.html")


@app.route("/api/status")
def api_status():
    """Everything the face animates against, in one poll."""
    return jsonify(status.snapshot())


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    """Answer a pending confirmation from the browser.

    The id is required and checked: whoever clicks is answering *that*
    question, not whatever happens to be pending by the time it arrives.
    """
    payload = request.get_json(silent=True) or {}
    try:
        confirmation_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "no confirmation id"}), 400

    answered = status.answer_pending(confirmation_id, bool(payload.get("approved")))
    return jsonify({"ok": answered, "reason": "" if answered else "already answered or expired"})


@app.route("/api/conversation")
def api_conversation():
    """The chat panel's contents, on the slow clock.

    Live if there is one. Otherwise the persisted transcript, because the face
    served by the daemon has no conversation of its own and an empty panel
    reads as "broken" rather than "nobody is talking to her right now".

    Deliberately not part of `/api/status`, which is polled several times a
    second to animate the core — this reads a file, and reading `session.json`
    off disk ten times a second is exactly what `/api/vitals` was split out to
    avoid.
    """
    live = status.snapshot()["transcript"]
    if live:
        return jsonify({"turns": live, "live": True})

    turns = []
    for message in session.load():
        content = message.get("content")
        role = "you" if message.get("role") == "user" else "ada"
        if isinstance(content, str):
            # The recap Ada writes to herself is bookkeeping, not conversation.
            if not content.startswith(recap.MARKER):
                turns.append({"role": role, "text": content, "streaming": False})
        elif isinstance(content, list):
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text.strip():
                turns.append({"role": role, "text": text, "streaming": False})
    return jsonify({"turns": turns[-12:], "live": False})


@app.route("/api/jobs")
def api_jobs():
    """Open work first, then what recently finished."""
    everything = jobs.load_jobs()
    open_jobs = [j for j in everything if j["state"] in jobs.OPEN]
    closed = [j for j in everything if j["state"] not in jobs.OPEN][-6:]

    def row(job):
        return {
            "id": job["id"],
            "task": job["task"],
            "state": job["state"],
            "line": jobs.describe(job),
            "spent": job.get("spent_usd", 0.0),
            "budget": job.get("budget_usd"),
            "tools": job.get("tools"),
            "attempts": job.get("attempts", 0),
            "at": job.get("finished_at") or job.get("started_at") or job.get("created_at"),
            "result": job.get("result"),
            "error": job.get("error"),
        }

    return jsonify({"open": [row(j) for j in open_jobs], "closed": [row(j) for j in reversed(closed)]})


@app.route("/api/approvals")
def api_approvals():
    return jsonify(
        [
            {
                "id": job["id"],
                "task": job["task"],
                "wants": approvals.describe(job),
                "tool": (job.get("pending_approval") or {}).get("tool"),
                "asked_at": (job.get("pending_approval") or {}).get("asked_at"),
            }
            for job in approvals.pending()
        ]
    )


@app.route("/api/approvals/<int:job_id>/<decision>", methods=["POST"])
def api_resolve_approval(job_id: int, decision: str):
    """Answer a parked job from the browser.

    The same decision the gated `resolve_approval` tool asks a human to make,
    reached the other way round. There's no second confirmation here and there
    shouldn't be: the tool is gated because a *model* might be calling it, and
    a click on a loopback-only page is a person deciding, which is the thing
    the gate was trying to get to in the first place.
    """
    if decision not in ("approve", "deny"):
        return jsonify({"ok": False, "reason": "decision must be approve or deny"}), 400

    try:
        (approvals.approve if decision == "approve" else approvals.deny)(job_id)
    except ValueError as e:
        return jsonify({"ok": False, "reason": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/api/schedules")
def api_schedules():
    return jsonify(
        [
            {
                "id": s["id"],
                "task": s["task"],
                "rhythm": schedules.describe(s["spec"]),
                "enabled": s.get("enabled", True),
                "next_due_at": s.get("next_due_at"),
                "runs": s.get("runs", 0),
            }
            for s in sorted(
                schedules.load_schedules(),
                key=lambda s: (not s.get("enabled", True), s.get("next_due_at", 0)),
            )
        ]
    )


@app.route("/api/positions")
def api_positions():
    """Holdings, from what the heartbeat last stored — never a live fetch.

    This endpoint is polled every few seconds by an open dashboard. Pricing
    each holding here would mean dozens of requests a minute to a free service
    that asks nothing of us, to show a number that moves in cents.
    `heartbeat/positions_check.py` reads prices every five minutes while the
    market is open; this serves what it wrote, and says when it was written so
    the page can't imply it's live.
    """
    rows = []
    for held in positions.load_positions():
        price = held.get("last_price")
        cost = held.get("cost")
        stop = held.get("stop")
        target = held.get("target")

        rows.append(
            {
                "symbol": held["symbol"],
                "shares": held["shares"],
                "cost": cost,
                "price": price,
                "priced_at": held.get("priced_at"),
                "stop": stop,
                "target": target,
                "pnl_pct": ((price - cost) / cost * 100) if price and cost else None,
                "pnl": ((price - cost) * held["shares"]) if price and cost else None,
                # Room left before each rule trips, as a percentage of the
                # current price — the number a holder actually reads.
                "to_stop_pct": ((price - stop) / price * 100) if price and stop else None,
                "to_target_pct": ((target - price) / price * 100) if price and target else None,
                "invalidation": held.get("invalidation"),
                "fired": sorted(held.get("fired", {})),
            }
        )

    # Closest to its stop first: that's the one worth looking at.
    rows.sort(key=lambda r: (r["to_stop_pct"] is None, r["to_stop_pct"] or 0))
    return jsonify(rows)


@app.route("/api/usage")
def api_usage():
    """The spend chart's series, plus the totals beside it."""
    days = request.args.get("days", type=int) or 30
    series = budget.daily(budget.MODEL, days=min(max(days, 7), 30))
    window = budget.WINDOWS["per_day"] if days <= 1 else days * 86400

    return jsonify(
        {
            "series": series,
            "days": days,
            "total": sum(point["amount"] for point in series),
            "today": budget.spent(budget.MODEL, budget.WINDOWS["per_day"]),
            "month": budget.spent(budget.MODEL, budget.WINDOWS["per_month"]),
            "window": window,
            "tokens": cost.totals(),
            "gauges": budget.gauges(),
        }
    )


@app.route("/api/notices")
def api_notices():
    return jsonify(list_notices())


@app.route("/api/notices/<int:notice_id>/dismiss", methods=["POST"])
def api_dismiss(notice_id: int):
    return jsonify({"ok": dismiss_notice(notice_id)})


@app.route("/api/heartbeat")
def api_heartbeat_status():
    return jsonify({"paused": is_paused()})


@app.route("/api/heartbeat/pause", methods=["POST"])
def api_heartbeat_pause():
    set_paused(True)
    return jsonify({"paused": True})


@app.route("/api/heartbeat/resume", methods=["POST"])
def api_heartbeat_resume():
    set_paused(False)
    return jsonify({"paused": False})


@app.route("/api/activity")
def api_activity():
    return jsonify(audit.tail(30))


@app.route("/api/vitals")
def api_vitals():
    """The slow-moving numbers, in one poll.

    Separate from `/api/status` because they move on different clocks: state
    has to be read several times a second to animate, while money and token
    counts change once a turn. Polling them together would mean either a
    sluggish face or reading `data/session.json` off disk ten times a second.
    """
    return jsonify(
        {
            "uptime": status.uptime(),
            "armed": status.armed(),
            "catalogue": len(tool_specs()),
            "messages": len(session.load()),
            "spend": budget.gauges(),
            "tokens": cost.totals(),
        }
    )
