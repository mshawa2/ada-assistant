"""The executor: the first time Ada does anything with tools unattended.

The paths worth pinning are the three ways a turn ends, the tool allowlist
being a boundary rather than a hint, and the money — a background job is not a
good enough reason to blow through a cap the user set.
"""

import pytest

from ada import agent, approvals, cost, jobs, safety
from ada.heartbeat import executor, notices
from ada.llm import LLMError
from ada.tools import reminders
from tests.conftest import FakeBlock, FakeMessage, fake_stream_turn


def _reply(text):
    return FakeMessage([FakeBlock("text", text=text)], "end_turn")


def _calls_tool(name, tool_input, then="All done."):
    return (
        FakeMessage([FakeBlock("tool_use", id="toolu_1", name=name, input=tool_input)], "tool_use"),
        _reply(then),
    )


def _queued(task="summarise this week's notes", **fields):
    job = jobs.create(task, **fields)
    return jobs.claim()


# --- finishing --------------------------------------------------------------


def test_a_finished_job_keeps_the_model_s_last_words_as_the_result(monkeypatch, sent_notifications):
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(_reply("Three notes, all about the boiler.")))
    job = _queued()

    assert executor.run_one(job) == jobs.DONE
    assert jobs.get(1)["result"] == "Three notes, all about the boiler."


def test_the_result_is_the_final_reply_not_the_running_commentary(monkeypatch, sent_notifications):
    """"Let me check that" is not a result."""
    monkeypatch.setattr(
        agent,
        "stream_turn",
        fake_stream_turn(
            FakeMessage(
                [FakeBlock("text", text="Let me look."), FakeBlock("tool_use", id="t1", name="list_reminders", input={})],
                "tool_use",
            ),
            _reply("You have nothing on."),
        ),
    )
    job = _queued()

    executor.run_one(job)

    assert jobs.get(1)["result"] == "You have nothing on."


def test_a_finished_job_tells_the_user(monkeypatch, sent_notifications):
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(_reply("Nothing needs doing.")))
    executor.run_one(_queued("check the notes"))

    raised = notices.list_notices()
    assert len(raised) == 1
    assert "Job #1 done" in raised[0]["text"]
    assert "Nothing needs doing." in raised[0]["text"]


def test_a_silent_model_still_produces_an_honest_result(monkeypatch, sent_notifications):
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(_reply("")))
    executor.run_one(_queued())

    assert "didn't say anything" in jobs.get(1)["result"]


def test_a_long_result_is_previewed_in_the_notice_not_pasted(monkeypatch, sent_notifications):
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(_reply("x" * 2000)))
    executor.run_one(_queued())

    assert len(notices.list_notices()[0]["text"]) < 600
    assert len(jobs.get(1)["result"]) == 2000, "the whole thing is still on the job"


# --- parking ----------------------------------------------------------------


def test_a_job_that_needs_approval_parks_instead_of_finishing(monkeypatch, sent_notifications):
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: True)
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_calls_tool("add_reminder", {"text": "buy milk"})))
    job = _queued("add a reminder")

    assert executor.run_one(job) == jobs.WAITING_APPROVAL

    stored = jobs.get(1)
    assert stored["state"] == jobs.WAITING_APPROVAL
    assert stored["result"] is None, "parking is not finishing"
    assert stored["pending_approval"]["tool"] == "add_reminder"
    assert reminders.load_reminders() == [], "the gated tool did not run"


def test_parking_does_not_raise_a_second_notice(monkeypatch, sent_notifications):
    """The approval notice went out when it parked; a 'job stopped' notice on
    top of it would say the same thing twice."""
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: True)
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_calls_tool("add_reminder", {"text": "x"})))

    executor.run_one(_queued())

    assert len(notices.list_notices()) == 1
    assert "needs your approval" in notices.list_notices()[0]["text"]


def test_an_approved_job_runs_the_action_through_on_its_next_turn(monkeypatch, sent_notifications):
    """The whole round trip: park, approve, re-claim, and the grant carries."""
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: True)
    script = lambda: _calls_tool("add_reminder", {"text": "buy milk"})  # noqa: E731

    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*script()))
    executor.run_one(_queued())
    approvals.approve(1)

    # A fresh script: the re-run starts its turn over, so the model asks for
    # the same tool again — which is exactly what the grant is matched against.
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*script()))
    assert executor.run_one(jobs.claim()) == jobs.DONE
    assert [r["text"] for r in reminders.load_reminders()] == ["buy milk"]


# --- breaking ---------------------------------------------------------------


def test_a_job_that_breaks_fails_with_the_error_kept(monkeypatch, sent_notifications):
    def explode(messages, tools):
        raise LLMError("Rate limited by the model provider.")

    monkeypatch.setattr(agent, "stream_turn", explode)

    assert executor.run_one(_queued()) == jobs.FAILED
    assert "Rate limited" in jobs.get(1)["error"]
    assert "failed" in notices.list_notices()[0]["text"]


def test_being_over_the_global_budget_fails_the_job_rather_than_spending(monkeypatch, sent_notifications):
    """LLMError is how a breached spending cap arrives. A background job is not
    a good enough reason to blow through a limit the user set."""

    def over(messages, tools):
        raise LLMError("Spending limit reached for 'model'.")

    monkeypatch.setattr(agent, "stream_turn", over)
    executor.run_one(_queued())

    assert jobs.get(1)["state"] == jobs.FAILED
    assert "Spending limit" in jobs.get(1)["error"]


# --- money ------------------------------------------------------------------


def test_a_job_that_is_out_of_budget_never_reaches_the_model(monkeypatch, sent_notifications):
    called = []
    monkeypatch.setattr(agent, "stream_turn", lambda *a, **k: called.append(1))

    job = _queued(budget_usd=0.10)
    jobs.record_spend(1, 0.10)

    assert executor.run_one(jobs.get(1)) == jobs.FAILED
    assert called == [], "checked before the money is spent, not after"
    assert "out of budget" in jobs.get(1)["error"]


def test_what_a_turn_costs_lands_on_the_job(monkeypatch, sent_notifications):
    def bills(messages, tools):
        def generator():
            cost.record(10_000, 1_000)
            return _reply("done")
            yield  # pragma: no cover — generator marker

        return generator()

    monkeypatch.setattr(agent, "stream_turn", bills)
    executor.run_one(_queued())

    assert jobs.get(1)["spent_usd"] > 0


def test_spending_accumulates_across_attempts():
    jobs.create("x")
    jobs.record_spend(1, 0.02)
    jobs.record_spend(1, 0.03)

    assert jobs.get(1)["spent_usd"] == pytest.approx(0.05)
    assert jobs.over_budget(jobs.get(1)) is False

    assert jobs.over_budget({"budget_usd": 0.04, "spent_usd": 0.05}) is True
    assert jobs.over_budget({"budget_usd": None, "spent_usd": 999}) is False, "no slice means no cap"


def test_the_meter_measures_only_its_own_thread_s_work():
    with cost.meter() as spent:
        cost.record(10_000, 1_000)
        inner = spent()
    cost.record(10_000, 1_000)

    with cost.meter() as spent:
        assert spent() == 0.0, "a new measurement starts from nothing"
    assert inner > 0


# --- the tool allowlist -----------------------------------------------------


def test_a_restricted_job_is_only_sent_the_tools_it_may_use(monkeypatch):
    fake = fake_stream_turn(_reply("ok"))
    monkeypatch.setattr(agent, "stream_turn", fake)

    agent.run_turn(
        [{"role": "user", "content": "x"}],
        on_text=lambda t: None,
        on_status=lambda s: None,
        confirm=lambda a: True,
        allowed_tools=["list_notes", "read_note"],
    )

    assert {spec["name"] for spec in fake.calls[0]["tools"]} == {"list_notes", "read_note"}


def test_a_restricted_job_cannot_reach_past_its_tool_list(monkeypatch):
    """The boundary, not the hint. Choosing what to send is a suggestion to a
    model; this is the part that actually refuses."""
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_calls_tool("add_reminder", {"text": "sneaky"})))

    messages = [{"role": "user", "content": "x"}]
    agent.run_turn(
        messages,
        on_text=lambda t: None,
        on_status=lambda s: None,
        confirm=lambda a: True,
        allowed_tools=["list_notes"],
    )

    assert reminders.load_reminders() == [], "the tool did not run"
    results = [
        block
        for message in messages
        if isinstance(message["content"], list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert results[0]["is_error"] is True
    assert "isn't available to this background job" in results[0]["content"]


def test_an_unrestricted_turn_still_routes_as_before(monkeypatch):
    """Conversation is untouched: None means the usual routing, which is an
    optimization and never a capability limit."""
    fake = fake_stream_turn(_reply("ok"))
    monkeypatch.setattr(agent, "stream_turn", fake)

    agent.run_turn(
        [{"role": "user", "content": "what's on my list?"}],
        on_text=lambda t: None,
        on_status=lambda s: None,
        confirm=lambda a: True,
    )

    names = {spec["name"] for spec in fake.calls[0]["tools"]}
    assert "find_tool" in names, "the escape hatch is there for a conversation"
    assert "list_reminders" in names


def test_a_restricted_job_gets_no_escape_hatch(monkeypatch):
    """find_tool would only show it things it still can't call."""
    fake = fake_stream_turn(_reply("ok"))
    monkeypatch.setattr(agent, "stream_turn", fake)

    agent.run_turn(
        [{"role": "user", "content": "x"}],
        on_text=lambda t: None,
        on_status=lambda s: None,
        confirm=lambda a: True,
        allowed_tools=["list_notes"],
    )

    assert "find_tool" not in {spec["name"] for spec in fake.calls[0]["tools"]}


# --- the check itself -------------------------------------------------------


def test_the_check_runs_one_job_a_tick_by_default(monkeypatch, sent_notifications):
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(_reply("ok")))
    jobs.create("first")
    jobs.create("second")

    executor.run_jobs({})

    assert jobs.get(1)["state"] == jobs.DONE
    assert jobs.get(2)["state"] == jobs.PENDING, "a slow job mustn't hold up the whole heartbeat"


def test_the_check_can_be_told_to_drain_more(monkeypatch, sent_notifications):
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(_reply("ok")))
    jobs.create("first")
    jobs.create("second")

    executor.run_jobs({"max_jobs_per_tick": 2})

    assert jobs.get(1)["state"] == jobs.DONE
    assert jobs.get(2)["state"] == jobs.DONE


def test_the_check_does_nothing_with_an_empty_queue(monkeypatch):
    called = []
    monkeypatch.setattr(agent, "stream_turn", lambda *a, **k: called.append(1))

    executor.run_jobs({"max_jobs_per_tick": 5})

    assert called == []


def test_a_job_not_yet_due_is_left_alone(monkeypatch, sent_notifications):
    import time

    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(_reply("ok")))
    jobs.create("later", run_at=time.time() + 3600)

    executor.run_jobs({})

    assert jobs.get(1)["state"] == jobs.PENDING


def test_the_executor_is_registered_as_a_heartbeat_check():
    from ada.heartbeat.checks import CHECKS

    assert CHECKS["run_jobs"] is executor.run_jobs


# --- what the model is told -------------------------------------------------


def test_the_job_prompt_says_nobody_is_there():
    """Without this it writes 'let me know if you'd like more', to nobody, and
    the stored result is a question."""
    prompt = executor._prompt({"id": 3, "task": "check the train times"})

    assert "check the train times" in prompt
    assert "unattended" in prompt
    assert "not here" in prompt
    assert "final reply" in prompt
