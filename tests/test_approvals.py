"""The gate with nobody behind it.

The interesting paths are the two that decide whether unattended work is safe:
that a background turn *stops* rather than guessing, and that a human's yes
authorises exactly one thing rather than opening a door.
"""

import pytest

from ada import agent, approvals, jobs, safety, settings
from ada.confirm import Action
from ada.heartbeat import greeting, notices
from ada.tools import approvals_tool
from tests.conftest import FakeBlock, FakeMessage, fake_stream_turn


def _parked_job(task="email the landlord", tool="send_message", tool_input=None, description="Text Sam: 'the boiler again'"):
    tool_input = {"to": "sam", "text": "the boiler again"} if tool_input is None else tool_input
    job = jobs.create(task)
    # claim() takes the oldest eligible job, which isn't necessarily this one —
    # an earlier job requeued by an approval is older. Drain until we hold it.
    while True:
        claimed = jobs.claim()
        if claimed is None or claimed["id"] == job["id"]:
            break
    approvals.park(job["id"], Action(tool=tool, input=tool_input, description=description))
    return jobs.get(job["id"])


# --- the headless gate ------------------------------------------------------


def test_an_unattended_gate_parks_the_job_and_stops_the_turn():
    """Not False — that means *declined*, which the model reasons about and
    works around. Parking is 'not yet', and stopping is the only faithful way
    to say that inside a synchronous turn."""
    jobs.create("email the landlord")
    jobs.claim()
    confirm = approvals.headless(1)

    with pytest.raises(approvals.Parked) as caught:
        confirm(Action(tool="send_message", input={"to": "sam"}, description="Text Sam"))

    assert caught.value.job_id == 1
    assert jobs.get(1)["state"] == jobs.WAITING_APPROVAL
    assert jobs.get(1)["pending_approval"]["tool"] == "send_message"


def test_parking_records_what_was_asked_precisely_enough_to_replay():
    job = _parked_job()

    approval = job["pending_approval"]
    assert approval["tool"] == "send_message"
    assert approval["input"] == {"to": "sam", "text": "the boiler again"}
    assert approval["description"] == "Text Sam: 'the boiler again'"
    assert approval["asked_at"] is not None


def test_parking_raises_an_urgent_notice(sent_notifications):
    _parked_job()

    raised = notices.list_notices()
    assert len(raised) == 1
    assert raised[0]["level"] == "urgent", "announced next session, held — not dropped — in quiet hours"
    assert "needs your approval" in raised[0]["text"]
    assert "the boiler again" in raised[0]["text"]


def test_an_approved_action_goes_through_once_when_the_job_runs_again():
    _parked_job()
    approvals.approve(1)
    jobs.claim()

    confirm = approvals.headless(1)
    action = Action(tool="send_message", input={"to": "sam", "text": "the boiler again"}, description="Text Sam")

    assert confirm(action) is True

    # ...and only once. A retry loop must not spend the same yes twice.
    with pytest.raises(approvals.Parked):
        confirm(action)


def test_an_approval_does_not_cover_a_different_message():
    """The whole point of matching on arguments: approving one text must not
    authorise texting the same person something else."""
    _parked_job()
    approvals.approve(1)
    jobs.claim()

    confirm = approvals.headless(1)
    with pytest.raises(approvals.Parked):
        confirm(Action(tool="send_message", input={"to": "sam", "text": "something else"}, description="Text Sam"))

    assert jobs.get(1)["state"] == jobs.WAITING_APPROVAL


# --- answering --------------------------------------------------------------


def test_approving_requeues_the_job_and_clears_the_question(sent_notifications):
    _parked_job()

    approvals.approve(1)

    assert jobs.get(1)["state"] == jobs.PENDING
    assert jobs.get(1)["granted"]["tool"] == "send_message"
    assert notices.list_notices() == [], "a question the user has settled stops being asked"


def test_denying_cancels_the_job(sent_notifications):
    _parked_job()

    approvals.deny(1)

    assert jobs.get(1)["state"] == jobs.CANCELLED
    assert "you declined" in jobs.get(1)["log"][-1]["note"]
    assert notices.list_notices() == []


def test_answering_a_job_that_is_not_waiting_is_a_clear_error():
    jobs.create("x")

    for answer in (approvals.approve, approvals.deny):
        with pytest.raises(ValueError, match="isn't waiting for approval"):
            answer(1)


def test_pending_lists_the_oldest_question_first():
    _parked_job(task="first", description="one")
    second = jobs.create("second")
    jobs.claim()
    approvals.park(second["id"], Action(tool="send_message", input={}, description="two"))

    assert [j["task"] for j in approvals.pending()] == ["first", "second"]


# --- through the real agent core --------------------------------------------


def test_a_background_turn_stops_at_the_gate_instead_of_guessing(monkeypatch):
    """End to end through run_turn: the gate is untouched Tier 6 code, and a
    headless confirm slots into it as a plain callable."""
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: True)
    monkeypatch.setattr(
        agent,
        "stream_turn",
        fake_stream_turn(
            FakeMessage([FakeBlock("tool_use", id="toolu_1", name="add_reminder", input={"text": "buy milk"})], "tool_use"),
            FakeMessage([FakeBlock("text", text="done")], "end_turn"),
        ),
    )
    jobs.create("add the thing")
    jobs.claim()

    with pytest.raises(approvals.Parked):
        agent.run_turn(
            [{"role": "user", "content": "x"}],
            on_text=lambda t: None,
            on_status=lambda s: None,
            confirm=approvals.headless(1),
        )

    assert jobs.get(1)["state"] == jobs.WAITING_APPROVAL
    from ada.tools import reminders

    assert reminders.load_reminders() == [], "the gated tool did not run"


# --- the tools --------------------------------------------------------------


def test_resolving_an_approval_is_itself_gated():
    """Gated because of who might be *calling* it. A web page saying 'approve
    job 3' is a document trying to send a text through Ada; this is what keeps
    the model able only to propose an answer."""
    assert settings.DEFAULTS["autonomy"]["tools"]["resolve_approval"] == settings.CONFIRM


def test_the_confirmation_prompt_shows_the_job_action_not_this_tool():
    """'Resolve approval #3' is not a decision anyone can make."""
    _parked_job()

    approve_prompt = approvals_tool._resolve_prompt({"id": 1, "decision": "approve"})
    deny_prompt = approvals_tool._resolve_prompt({"id": 1, "decision": "deny"})

    assert "the boiler again" in approve_prompt
    assert "once" in approve_prompt
    assert "the boiler again" in deny_prompt
    assert "cancelled" in deny_prompt


def test_the_prompt_survives_an_id_that_does_not_exist():
    """A broken description must never swallow the gate itself."""
    assert "an action" in approvals_tool._resolve_prompt({"id": 99, "decision": "approve"})


def test_listing_approvals_says_what_each_job_wants():
    _parked_job()

    listed = approvals_tool.list_approvals()

    assert "#1" in listed
    assert "email the landlord" in listed
    assert "the boiler again" in listed


def test_listing_approvals_when_there_are_none():
    assert "Nothing is waiting" in approvals_tool.list_approvals()


def test_the_tool_approves_and_denies(sent_notifications):
    _parked_job()
    assert "Approved" in approvals_tool.resolve_approval(1, "approve")
    assert jobs.get(1)["state"] == jobs.PENDING

    _parked_job(task="second")
    assert "Refused" in approvals_tool.resolve_approval(2, "deny")
    assert jobs.get(2)["state"] == jobs.CANCELLED


def test_the_tool_rejects_a_decision_it_does_not_understand():
    _parked_job()

    with pytest.raises(ValueError, match="isn't a decision"):
        approvals_tool.resolve_approval(1, "maybe")

    assert jobs.get(1)["state"] == jobs.WAITING_APPROVAL


# --- the greeting -----------------------------------------------------------


def test_the_greeting_leads_with_what_is_blocked(sent_notifications):
    _parked_job()

    summary = greeting.startup_summary()

    assert summary.startswith("1 job stopped waiting for you")
    assert "the boiler again" in summary


def test_a_dismissed_notice_does_not_hide_a_job_still_waiting(sent_notifications):
    """The notice log is what Ada observed; a pending approval is work that is
    stopped. Dismissing the first must not make the second invisible."""
    _parked_job()
    for notice in notices.list_notices():
        notices.dismiss_notice(notice["id"])

    assert "stopped waiting for you" in greeting.startup_summary()


def test_the_greeting_says_it_once(sent_notifications):
    """Parking raises a notice *and* blocks the job. The greeting has both
    sources and must not read them both out."""
    _parked_job()

    summary = greeting.startup_summary()

    assert summary.count("the boiler again") == 1
    assert "While you were away" not in summary


def test_the_greeting_stays_quiet_with_nothing_to_say():
    assert greeting.startup_summary() is None
