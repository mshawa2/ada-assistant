"""The agent core: the confirmation gate, the runaway-tool-call cap, and
truncated replies. These are the paths where a regression is expensive — the
gate is the whole of Tier 6, and the cap is what stops a loop."""

import pytest

from ada import agent, safety
from ada.tools import reminders
from tests.conftest import FakeBlock, FakeMessage, fake_stream_turn


@pytest.fixture
def captured():
    return {"text": [], "status": []}


def _run(messages, captured, confirm=lambda _: True):
    return agent.run_turn(
        messages,
        on_text=captured["text"].append,
        on_status=captured["status"].append,
        confirm=confirm,
    )


def _tool_then_done(tool_name, tool_input, reply="Done."):
    return (
        FakeMessage([FakeBlock("tool_use", id="toolu_1", name=tool_name, input=tool_input)], "tool_use"),
        FakeMessage([FakeBlock("text", text=reply)], "end_turn"),
    )


def _tool_results(messages):
    return [
        block
        for message in messages
        if isinstance(message["content"], list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def test_plain_reply_streams_and_appends_one_assistant_message(monkeypatch, captured):
    monkeypatch.setattr(
        agent, "stream_turn", fake_stream_turn(FakeMessage([FakeBlock("text", text="hi there")], "end_turn"))
    )
    messages = [{"role": "user", "content": "hello"}]

    _run(messages, captured)

    assert "".join(captured["text"]) == "hi there"
    assert len(messages) == 2
    assert messages[-1]["role"] == "assistant"


def test_ungated_tool_runs_without_asking(monkeypatch, captured):
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_tool_then_done("add_reminder", {"text": "buy milk"})))

    def refuse(_description):
        raise AssertionError("an ungated tool must never prompt for confirmation")

    _run([{"role": "user", "content": "remind me"}], captured, confirm=refuse)

    assert [r["text"] for r in reminders.load_reminders()] == ["buy milk"]


def test_gated_tool_does_not_run_when_declined(monkeypatch, captured):
    """Declining must leave the action never having happened at all."""
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: name == "add_reminder")
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_tool_then_done("add_reminder", {"text": "buy milk"})))

    messages = [{"role": "user", "content": "remind me"}]
    _run(messages, captured, confirm=lambda _: False)

    assert reminders.load_reminders() == []
    declined = _tool_results(messages)
    assert declined and declined[0]["is_error"] is True
    assert "declined" in declined[0]["content"]


def test_gated_tool_runs_when_approved(monkeypatch, captured):
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: name == "add_reminder")
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_tool_then_done("add_reminder", {"text": "buy milk"})))

    _run([{"role": "user", "content": "remind me"}], captured, confirm=lambda _: True)

    assert [r["text"] for r in reminders.load_reminders()] == ["buy milk"]


def test_confirmation_prompt_names_the_tool_and_its_arguments(monkeypatch, captured):
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: True)
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_tool_then_done("add_reminder", {"text": "buy milk"})))
    asked = []

    _run([{"role": "user", "content": "x"}], captured, confirm=lambda d: asked.append(d) or False)

    assert "add_reminder" in asked[0].description
    assert "buy milk" in asked[0].description
    # The gate is handed the action itself, not just a sentence about it —
    # an unattended gate has to record what was asked for precisely enough to
    # recognise the same request coming back after a human approves it.
    assert asked[0].tool == "add_reminder"
    assert asked[0].input == {"text": "buy milk"}


def test_tool_error_comes_back_as_an_error_result_not_a_crash(monkeypatch, captured):
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(*_tool_then_done("complete_reminder", {"id": 999})))

    messages = [{"role": "user", "content": "done with 999"}]
    _run(messages, captured)

    assert any(r.get("is_error") for r in _tool_results(messages)), "a failing tool reports back, never raises"


def test_runaway_tool_calls_stop_at_the_cap_with_a_valid_transcript(monkeypatch, captured):
    """A model that never stops calling tools must not loop forever, and the
    message list it leaves behind must still be a legal request."""
    forever = FakeMessage([FakeBlock("tool_use", id="toolu_x", name="list_reminders", input={})], "tool_use")
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(forever))

    messages = [{"role": "user", "content": "loop"}]
    _run(messages, captured)

    assert any("too many tool calls" in s for s in captured["status"])
    assert messages[-1]["role"] == "user"
    cancelled = _tool_results([messages[-1]])
    assert cancelled and cancelled[0]["is_error"] is True


def test_truncated_reply_is_announced_rather_than_passed_off_as_complete(monkeypatch, captured):
    monkeypatch.setattr(
        agent,
        "stream_turn",
        fake_stream_turn(FakeMessage([FakeBlock("text", text="a long answer that got cut")], "max_tokens")),
    )

    _run([{"role": "user", "content": "summarize"}], captured)

    assert any("length limit" in s for s in captured["status"])
