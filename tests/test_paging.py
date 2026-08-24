"""Context paging: what happens to the parts that don't fit.

Two halves of the same problem. A conversation outgrows its window, and a job
outgrows a single turn — in both cases the cheap answer is to throw the old
part away, and in both cases that loses work that was actually done.
"""

import pytest

from ada import agent, approvals, jobs, recap, safety, session
from ada.heartbeat import executor
from ada.llm import LLMError
from tests.conftest import FakeBlock, FakeMessage, fake_stream_turn


def _exchange(n):
    return [
        {"role": "user", "content": f"question {n}"},
        {"role": "assistant", "content": [{"type": "text", "text": f"answer {n}"}]},
    ]


def _conversation(exchanges):
    return [m for n in range(exchanges) for m in _exchange(n)]


# --- the session prefix -----------------------------------------------------


def test_a_short_conversation_is_left_alone(monkeypatch):
    called = []
    monkeypatch.setattr(recap, "summarize", lambda m: called.append(m))
    messages = _conversation(3)

    session.record(messages)

    assert len(messages) == 6
    assert called == [], "nothing fell off, so there's nothing to summarize"


def test_what_falls_off_is_summarized_rather_than_dropped(monkeypatch):
    seen = {}

    def fake(dropped):
        seen["dropped"] = dropped
        return "They asked about trains and decided on the 8:15."

    monkeypatch.setattr(recap, "summarize", fake)
    messages = _conversation(40)

    session.record(messages)

    assert messages[0]["content"].startswith(recap.MARKER)
    assert "the 8:15" in messages[0]["content"]
    assert seen["dropped"], "the dropped prefix is what got summarized"
    assert len(messages) <= session.MAX_MESSAGES + 2


def test_the_recap_keeps_the_turns_alternating(monkeypatch):
    """A lone user message would leave two user turns back to back where the
    kept transcript starts with the user's own words."""
    monkeypatch.setattr(recap, "summarize", lambda m: "notes")
    messages = _conversation(40)

    session.record(messages)

    assert [m["role"] for m in messages[:3]] == ["user", "assistant", "user"]


def test_the_recap_rolls_forward_instead_of_stacking(monkeypatch):
    """It rides at the front, so the next trim summarizes it along with
    whatever else fell off."""
    summarized = []

    def fake(dropped):
        summarized.append(dropped)
        return f"recap {len(summarized)}"

    monkeypatch.setattr(recap, "summarize", fake)

    messages = _conversation(40)
    session.record(messages)
    messages.extend(_conversation(40))
    session.record(messages)

    markers = [m for m in messages if isinstance(m["content"], str) and m["content"].startswith(recap.MARKER)]
    assert len(markers) == 1, "one recap, not one per trim"
    assert "recap 2" in markers[0]["content"]
    assert any(
        isinstance(m.get("content"), str) and m["content"].startswith(recap.MARKER)
        for m in summarized[1]
    ), "the second summarization saw the first recap"


def test_a_summary_that_cannot_be_produced_falls_back_to_dropping(monkeypatch):
    """Losing context is a bad outcome. Refusing to talk is a worse one."""
    monkeypatch.setattr(recap, "summarize", lambda m: None)
    messages = _conversation(40)

    session.record(messages)

    assert len(messages) <= session.MAX_MESSAGES
    assert not messages[0]["content"].startswith(recap.MARKER)


def test_the_recap_survives_a_restart(monkeypatch):
    monkeypatch.setattr(recap, "summarize", lambda m: "they like the aisle seat")
    messages = _conversation(40)
    session.record(messages)

    assert "aisle seat" in session.load()[0]["content"]


def test_the_resumed_line_does_not_count_the_recap_as_an_exchange(monkeypatch):
    monkeypatch.setattr(recap, "summarize", lambda m: "notes")
    messages = _conversation(40)
    session.record(messages)

    line = session.resumed_summary(messages)

    real_exchanges = sum(
        1 for m in messages
        if m["role"] == "user" and isinstance(m["content"], str) and not m["content"].startswith(recap.MARKER)
    )
    assert f"{real_exchanges} exchange" in line


# --- the summarizer itself --------------------------------------------------


def test_the_summarizer_asks_about_what_was_said_not_how(monkeypatch):
    asked = {}

    def fake_complete(prompt, max_tokens=512):
        asked["prompt"] = prompt
        return "  They booked the 8:15.  "

    monkeypatch.setattr(recap, "complete", fake_complete)

    result = recap.summarize(
        [
            {"role": "user", "content": "book me a train"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "web_search", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "a wall of html"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "The 8:15 works."}]},
        ]
    )

    assert result == "They booked the 8:15."
    assert "book me a train" in asked["prompt"]
    assert "used the web_search tool" in asked["prompt"]
    assert "a wall of html" not in asked["prompt"], "tool output is mentioned, not reproduced"


def test_the_summarizer_fails_soft(monkeypatch, capsys):
    def broken(prompt, max_tokens=512):
        raise LLMError("provider down")

    monkeypatch.setattr(recap, "complete", broken)

    assert recap.summarize([{"role": "user", "content": "hi"}]) is None
    assert "couldn't summarize" in capsys.readouterr().out


def test_nothing_worth_summarizing_costs_nothing(monkeypatch):
    monkeypatch.setattr(recap, "complete", lambda *a, **k: pytest.fail("should not have been called"))

    assert recap.summarize([]) is None


# --- job transcripts --------------------------------------------------------


def _tool_then_done(name, tool_input):
    return (
        FakeMessage([FakeBlock("tool_use", id="toolu_1", name=name, input=tool_input)], "tool_use"),
        FakeMessage([FakeBlock("text", text="All done.")], "end_turn"),
    )


def test_a_parked_job_keeps_where_it_got_to(monkeypatch, sent_notifications):
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: True)
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_tool_then_done("add_reminder", {"text": "x"})))
    jobs.create("do a thing")
    jobs.claim()

    executor.run_one(jobs.get(1))

    transcript = jobs.get(1)["transcript"]
    assert transcript, "the work so far is kept, not thrown away"
    assert transcript[0]["role"] == "user"
    assert "Background job #1" in transcript[0]["content"]


def test_the_interrupted_ask_is_stripped_so_the_transcript_can_be_sent_back():
    """The API rejects a dangling tool call, and that block is exactly the
    action that was interrupted."""
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "search", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "found"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "b", "name": "send_message", "input": {}}]},
    ]

    resumable = executor._resumable(messages)

    assert len(resumable) == 3
    assert resumable[-1]["role"] == "user", "ends on an answered tool call"


def test_a_finished_turn_is_kept_whole():
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]

    assert executor._resumable(messages) == messages


def test_an_approved_job_picks_up_instead_of_starting_over(monkeypatch, sent_notifications):
    """The rough edge this tier exists to close: a job that searched three
    times before hitting the gate shouldn't search three times again."""
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: name == "add_reminder")
    monkeypatch.setattr(
        agent,
        "stream_turn",
        fake_stream_turn(
            FakeMessage([FakeBlock("tool_use", id="t1", name="list_notes", input={})], "tool_use"),
            FakeMessage([FakeBlock("tool_use", id="t2", name="add_reminder", input={"text": "buy milk"})], "tool_use"),
        ),
    )
    jobs.create("look then act")
    jobs.claim()
    executor.run_one(jobs.get(1))

    assert jobs.get(1)["state"] == jobs.WAITING_APPROVAL
    approvals.approve(1)

    resumed = fake_stream_turn(
        FakeMessage([FakeBlock("tool_use", id="t3", name="add_reminder", input={"text": "buy milk"})], "tool_use"),
        FakeMessage([FakeBlock("text", text="Added it.")], "end_turn"),
    )
    monkeypatch.setattr(agent, "stream_turn", resumed)

    executor.run_one(jobs.claim())

    sent_back = resumed.calls[0]["messages"]
    assert len(sent_back) > 1, "it resumed with history rather than a bare prompt"
    assert any(
        isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"] if isinstance(b, dict))
        for m in sent_back
    ), "the earlier tool round came back with it"
    assert jobs.get(1)["state"] == jobs.DONE


def test_a_job_waiting_out_a_backoff_keeps_its_transcript(monkeypatch, sent_notifications):
    """A job that broke on its fourth search shouldn't redo the first three."""
    monkeypatch.setattr(agent, "stream_turn", lambda m, tools: (_ for _ in ()).throw(LLMError("flaky")))
    jobs.create("x")
    jobs.claim()

    executor.run_one(jobs.get(1))

    assert jobs.get(1)["state"] == jobs.PENDING
    assert jobs.get(1)["transcript"], "kept for the retry"


def test_a_finished_job_drops_its_transcript(monkeypatch, sent_notifications):
    """It's a resume point, not a record — `result` and `log` are the record."""
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(FakeMessage([FakeBlock("text", text="ok")], "end_turn")))
    jobs.create("x")
    jobs.claim()
    jobs.save_transcript(1, [{"role": "user", "content": "stale"}])

    executor.run_one(jobs.get(1))

    assert jobs.get(1)["transcript"] is None


def test_a_cancelled_job_drops_its_transcript():
    jobs.create("x")
    jobs.claim()
    jobs.save_transcript(1, [{"role": "user", "content": "half done"}])

    jobs.cancel(1)

    assert jobs.get(1)["transcript"] is None


# --- a torn round -----------------------------------------------------------
#
# Found by running a real job: the model routinely asks for several tools in
# one message. Dropping the whole round to get rid of the unanswered ask threw
# away work that had already happened — in the live run, a weather lookup that
# had already returned.


def _two_tools_at_once(first, second):
    return FakeMessage(
        [
            FakeBlock("tool_use", id="t1", name=first, input={}),
            FakeBlock("tool_use", id="t2", name=second, input={"text": "x"}),
        ],
        "tool_use",
    )


def test_work_done_before_the_gate_is_not_thrown_away(monkeypatch, sent_notifications):
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: name == "add_reminder")
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(_two_tools_at_once("list_notes", "add_reminder")))
    jobs.create("look then act")
    jobs.claim()

    executor.run_one(jobs.get(1))

    transcript = jobs.get(1)["transcript"]
    assert len(transcript) == 3, "prompt, the assistant's asks, and the results it got"
    assert transcript[-1]["role"] == "user"

    results = {r["tool_use_id"]: r["content"] for r in transcript[-1]["content"]}
    assert "No notes" in results["t1"] or results["t1"], "the tool that ran kept its real result"
    assert "APPROVED" in results["t2"] and "Call this tool again" in results["t2"], (
        "this message is only ever re-read after an approval, so it has to say so "
        "outright — a real run read 'they have answered' as a refusal and gave up"
    )


def test_every_ask_in_a_torn_round_gets_an_answer(monkeypatch, sent_notifications):
    """Every tool_use block must be answered or the whole transcript is
    rejected on the way back in."""
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: name == "add_reminder")
    monkeypatch.setattr(
        agent,
        "stream_turn",
        fake_stream_turn(
            FakeMessage(
                [
                    FakeBlock("tool_use", id="t1", name="list_notes", input={}),
                    FakeBlock("tool_use", id="t2", name="add_reminder", input={"text": "x"}),
                    FakeBlock("tool_use", id="t3", name="list_drafts", input={}),
                ],
                "tool_use",
            )
        ),
    )
    jobs.create("three at once")
    jobs.claim()

    executor.run_one(jobs.get(1))

    transcript = jobs.get(1)["transcript"]
    asked = {b["id"] for b in transcript[-2]["content"] if b["type"] == "tool_use"}
    answered = {r["tool_use_id"] for r in transcript[-1]["content"]}
    assert asked == answered

    results = {r["tool_use_id"]: r["content"] for r in transcript[-1]["content"]}
    assert "stopped to ask" in results["t3"], "the one behind the gate says why it didn't run"


def test_without_partial_results_the_torn_round_still_comes_off():
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "search", "input": {}}]},
    ]

    assert executor._resumable(messages) == messages[:1]
