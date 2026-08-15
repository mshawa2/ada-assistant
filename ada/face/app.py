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
"""

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from ada import audit, status
from ada.heartbeat.kill_switch import is_paused, set_paused
from ada.heartbeat.notices import dismiss_notice, list_notices

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
