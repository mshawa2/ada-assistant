"""The interface: live state, the conversation buffer, and confirmations that
either the terminal or the browser can answer.

The confirmation race is the part worth pinning down. `AGENT.md` recorded it as
deferred precisely because it's fiddly, and getting it wrong either loses the
answer or steals the user's next keystroke.
"""

import logging
import threading
import time
import types

import pytest

from ada import status
from ada.face import server as face_server
from ada.face.app import app


@pytest.fixture(autouse=True)
def clean_state():
    status.clear_transcript()
    status.end_confirmation()
    status.set_status("idle")
    status.set_armed([])
    yield
    status.clear_transcript()
    status.end_confirmation()
    status.set_armed([])


@pytest.fixture
def client():
    return app.test_client()


# --- the state a face animates against --------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("idle", status.IDLE),
        ("listening...", status.LISTENING),
        ('listening for "Ada"...', status.LISTENING),
        ("transcribing...", status.TRANSCRIBING),
        ("thinking...", status.THINKING),
        ("using send_message", status.TOOL),
        ("speaking...", status.SPEAKING),
        ("something nobody planned for", status.IDLE),
    ],
)
def test_status_strings_become_states_the_front_end_can_animate(raw, expected):
    """The face never pattern-matches on prose — the derivation lives here."""
    status.set_status(raw)

    assert status.snapshot()["state"] == expected


def test_a_running_tool_is_named_separately():
    status.set_status("using send_message")

    assert status.snapshot()["tool"] == "send_message"
    assert status.current_tool() == "send_message"


def test_a_non_tool_status_clears_the_tool():
    status.set_status("using send_message")
    status.set_status("thinking...")

    assert status.snapshot()["tool"] is None


def test_waiting_on_a_human_outranks_whatever_else_is_happening():
    """The orb must never look busy while it's actually blocked on you."""
    status.set_status("thinking...")
    status.begin_confirmation("Ada wants to do something")

    assert status.snapshot()["state"] == status.WAITING


# --- the conversation buffer ------------------------------------------------


def test_turns_appear_in_order():
    status.say("you", "what's on today?")
    status.say("ada", "Two things.")

    assert [t["role"] for t in status.transcript()] == ["you", "ada"]


def test_a_reply_streams_in_and_is_marked_while_it_does():
    status.begin_reply()
    status.stream("Two ")
    status.stream("things.")

    live = status.transcript()[-1]
    assert live["text"] == "Two things."
    assert live["streaming"] is True

    status.end_reply()
    assert status.transcript()[-1]["streaming"] is False


def test_an_empty_reply_is_dropped_rather_than_left_hanging():
    """A turn that errored before producing text shouldn't leave a blank
    bubble with a blinking cursor in it forever."""
    status.begin_reply()
    status.end_reply()

    assert status.transcript() == []


def test_blank_turns_are_ignored():
    status.say("you", "   ")

    assert status.transcript() == []


def test_the_buffer_is_bounded():
    """It's a display buffer, not history — that lives in session.json."""
    for i in range(status.MAX_TRANSCRIPT + 20):
        status.say("you", f"line {i}")

    assert len(status.transcript()) == status.MAX_TRANSCRIPT


# --- confirmations, from either side ----------------------------------------


def test_the_browser_can_answer_a_confirmation():
    pending = status.begin_confirmation("Ada wants to send a message")

    assert status.answer_pending(pending.id, True) is True
    assert pending.wait(timeout=1) is True


def test_declining_from_the_browser_declines():
    pending = status.begin_confirmation("Ada wants to send a message")
    status.answer_pending(pending.id, False)

    assert pending.wait(timeout=1) is False


def test_the_first_answer_wins():
    """Two people, or a click and a keypress, must not fight."""
    pending = status.begin_confirmation("x")

    assert pending.answer(True) is True
    assert pending.answer(False) is False, "the second answer is ignored"
    assert pending.approved is True


def test_a_stale_tab_cannot_answer_a_newer_question():
    """The id is what stops an old click approving a different action."""
    first = status.begin_confirmation("send a message")
    first.answer(False)
    second = status.begin_confirmation("spend $200")

    assert status.answer_pending(first.id, True) is False
    assert second.answered.is_set() is False


def test_waiting_with_nobody_there_declines():
    pending = status.begin_confirmation("x")

    assert pending.wait(timeout=0.05) is False


def test_a_confirmation_answered_from_another_thread_unblocks_the_waiter():
    """This is the whole mechanism: the agent blocks, the browser answers."""
    pending = status.begin_confirmation("Ada wants to send a message")
    result = {}

    def waiter():
        result["approved"] = pending.wait(timeout=2)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    status.answer_pending(pending.id, True)
    thread.join(timeout=2)

    assert result["approved"] is True


def test_the_legacy_helpers_still_work():
    """face/app.py's original shape, kept so nothing had to change at once."""
    status.set_pending_confirmation("do a thing")
    assert status.get_pending_confirmation() == "do a thing"

    status.set_pending_confirmation(None)
    assert status.get_pending_confirmation() is None


# --- the HTTP surface -------------------------------------------------------


def test_the_status_endpoint_returns_everything_the_face_needs(client):
    status.set_status("using send_message")
    status.say("you", "text Sam")

    body = client.get("/api/status").get_json()

    assert set(body) == {"status", "state", "tool", "transcript", "pending"}
    assert body["state"] == status.TOOL
    assert body["tool"] == "send_message"
    assert body["transcript"][0]["text"] == "text Sam"


def test_confirming_over_http_resolves_the_pending_action(client):
    pending = status.begin_confirmation("Ada wants to send a message")

    body = client.post("/api/confirm", json={"id": pending.id, "approved": True}).get_json()

    assert body["ok"] is True
    assert pending.wait(timeout=1) is True


def test_confirming_with_a_wrong_id_is_refused(client):
    pending = status.begin_confirmation("Ada wants to send a message")

    body = client.post("/api/confirm", json={"id": pending.id + 99, "approved": True}).get_json()

    assert body["ok"] is False
    assert pending.answered.is_set() is False


def test_confirming_with_no_id_is_a_bad_request(client):
    assert client.post("/api/confirm", json={"approved": True}).status_code == 400


def test_confirming_when_nothing_is_pending_is_refused(client):
    assert client.post("/api/confirm", json={"id": 1, "approved": True}).get_json()["ok"] is False


def test_the_page_is_served(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"orb" in response.data


@pytest.mark.parametrize(
    "route", ["/api/status", "/api/notices", "/api/heartbeat", "/api/activity", "/api/vitals"]
)
def test_every_read_endpoint_answers(client, route):
    assert client.get(route).status_code == 200


# --- the readouts the deck draws --------------------------------------------


def test_the_tools_routing_picked_are_visible(client):
    """Routing narrows 50-odd tools to a handful, and which handful is the one
    thing you want to see when a tool you expected wasn't used."""
    status.set_armed(["get_calendar_events", "find_tool"])

    body = client.get("/api/vitals").get_json()

    assert body["armed"] == ["get_calendar_events", "find_tool"]
    assert body["catalogue"] >= len(body["armed"])


def test_vitals_carries_every_number_the_deck_draws(client):
    body = client.get("/api/vitals").get_json()

    assert set(body) == {"uptime", "armed", "catalogue", "messages", "spend", "tokens"}
    assert set(body["tokens"]) == {"input", "output", "cached", "written", "cost"}
    assert body["uptime"] >= 0


def test_the_face_keeps_its_request_log_out_of_adas_output(monkeypatch):
    """The face shares stdout with the prompt you type to Ada, and polls five
    endpoints — the liveliest several times a second. Logging those buries the
    conversation in `GET /api/status`."""
    logging.getLogger("werkzeug").setLevel(logging.INFO)
    # No socket: this asserts on the wiring, not on claiming port 4173.
    monkeypatch.setattr(
        face_server,
        "make_server",
        lambda *a, **k: types.SimpleNamespace(serve_forever=lambda: None),
    )

    face_server.start().join(timeout=2)

    assert logging.getLogger("werkzeug").level >= logging.ERROR


def test_status_stays_the_shape_the_face_animates_against(client):
    """The slow numbers went into /api/vitals precisely so this stayed small —
    it's polled several times a second."""
    body = client.get("/api/status").get_json()

    assert set(body) == {"status", "state", "tool", "transcript", "pending"}
