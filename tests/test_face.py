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


# --- the OS half of the dashboard -------------------------------------------
#
# The face was built when Ada was an assistant. Jobs, approvals and schedules
# arrived after it, and until these endpoints existed none of it was on the
# page — the only trace of a blocked job was a *notice*, whose "clear" button
# dismissed the notice and left the job blocked forever.


@pytest.fixture
def client():
    return app.test_client()


def test_jobs_are_split_into_what_is_open_and_what_just_finished(client):
    from ada import jobs

    jobs.create("still going")
    jobs.create("finished")
    jobs.claim()
    jobs.claim()
    jobs.finish(2, "all done")

    body = client.get("/api/jobs").get_json()

    assert [j["task"] for j in body["open"]] == ["still going"]
    assert [j["task"] for j in body["closed"]] == ["finished"]
    assert body["open"][0]["state"] == "running"


def test_a_job_carries_the_numbers_the_panel_shows(client):
    from ada import jobs

    jobs.create("costly", tools=["web_search"], budget_usd=0.5)
    jobs.claim()
    jobs.record_spend(1, 0.031)

    row = client.get("/api/jobs").get_json()["open"][0]

    assert row["spent"] == pytest.approx(0.031)
    assert row["budget"] == 0.5
    assert row["tools"] == ["web_search"]


def test_a_blocked_job_is_reachable_from_the_browser(client, sent_notifications):
    """With the daemon running and no terminal open, a parked job had no way to
    be answered at all — you had to start a REPL to unblock it."""
    from ada import approvals, jobs
    from ada.confirm import Action

    jobs.create("email the landlord")
    jobs.claim()
    approvals.park(1, Action(tool="compose_email", input={"to": "sam"}, description="Email Sam"))

    listed = client.get("/api/approvals").get_json()
    assert [a["wants"] for a in listed] == ["Email Sam"]

    assert client.post("/api/approvals/1/approve").get_json()["ok"] is True
    assert jobs.get(1)["state"] == jobs.PENDING
    assert jobs.get(1)["granted"]["tool"] == "compose_email"
    assert client.get("/api/approvals").get_json() == []


def test_refusing_from_the_browser_stops_the_job(client, sent_notifications):
    from ada import approvals, jobs
    from ada.confirm import Action

    jobs.create("email the landlord")
    jobs.claim()
    approvals.park(1, Action(tool="compose_email", input={}, description="Email Sam"))

    assert client.post("/api/approvals/1/deny").get_json()["ok"] is True
    assert jobs.get(1)["state"] == jobs.CANCELLED


def test_answering_a_job_that_is_not_waiting_is_refused_not_crashed(client):
    from ada import jobs

    jobs.create("not blocked")

    response = client.post("/api/approvals/1/approve")
    assert response.status_code == 409
    assert "isn't waiting" in response.get_json()["reason"]


def test_only_approve_and_deny_are_decisions(client):
    assert client.post("/api/approvals/1/maybe").status_code == 400


def test_schedules_come_back_soonest_first_with_paused_ones_last(client):
    from ada import schedules

    schedules.create("weekly review", {"repeat": "weekly", "on": "sunday", "at": "18:00"})
    schedules.create("morning briefing", {"repeat": "daily", "at": "08:00"})
    schedules.create("hourly sweep", {"repeat": "hourly"})
    schedules.set_enabled(3, False)

    listed = client.get("/api/schedules").get_json()

    assert [s["task"] for s in listed][-1] == "hourly sweep", "paused ones sink"
    assert listed[0]["next_due_at"] <= listed[1]["next_due_at"]
    assert listed[0]["rhythm"]


# --- the conversation, when nothing is live ---------------------------------


def test_a_live_conversation_is_used_when_there_is_one(client):
    status.say("you", "hello")

    body = client.get("/api/conversation").get_json()

    assert body["live"] is True
    assert body["turns"][0]["text"] == "hello"


def test_the_persisted_transcript_stands_in_when_nothing_is_live(client):
    """The face served by the daemon has no conversation of its own, and an
    empty panel reads as "broken" rather than "nobody is talking to her"."""
    from ada import session

    session.record(
        [
            {"role": "user", "content": "what's the weather"},
            {"role": "assistant", "content": [{"type": "text", "text": "Clear and 24C."}]},
        ]
    )

    body = client.get("/api/conversation").get_json()

    assert body["live"] is False
    assert [t["text"] for t in body["turns"]] == ["what's the weather", "Clear and 24C."]
    assert [t["role"] for t in body["turns"]] == ["you", "ada"]


def test_the_recap_ada_writes_to_herself_is_not_conversation(client):
    from ada import recap, session

    session.record(
        [
            {"role": "user", "content": f"{recap.MARKER}\nThey prefer aisle seats."},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "book the train"},
        ]
    )

    turns = client.get("/api/conversation").get_json()["turns"]

    assert not any(recap.MARKER in t["text"] for t in turns)
    assert turns[-1]["text"] == "book the train"


def test_tool_traffic_is_not_shown_as_something_someone_said(client):
    from ada import session

    session.record(
        [
            {"role": "user", "content": "check it"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "get_weather", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "24C"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "It's 24C."}]},
        ]
    )

    turns = client.get("/api/conversation").get_json()["turns"]

    assert [t["text"] for t in turns] == ["check it", "It's 24C."]
