from pathlib import Path

from flask import Flask, jsonify, send_from_directory

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
    return jsonify(
        {
            "status": status.get_status(),
            "pending_confirmation": status.get_pending_confirmation(),
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
