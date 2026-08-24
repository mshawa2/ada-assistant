"""Two sides, argued independently, checked against what you already said.

The invariants worth pinning are the ones that stop this becoming a tipster:
the sides can't see each other, the research can't touch anything gated, the
synthesis can't go looking for more, and the holder's own words reach the
briefing.
"""

import pytest

from ada import agent, debate, positions, safety, settings
from ada.confirm import Action
from tests.conftest import FakeBlock, FakeMessage


def _script(*replies):
    """A stream_turn that answers each successive turn with the next reply."""
    calls = []

    def stream_turn(messages, tools):
        calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        message = FakeMessage([FakeBlock("text", text=replies[min(len(calls) - 1, len(replies) - 1)])], "end_turn")

        def generator():
            yield message.content[0].text
            return message

        return generator()

    stream_turn.calls = calls
    return stream_turn


# --- independence -----------------------------------------------------------


def test_each_side_argues_in_its_own_context(monkeypatch):
    """One model asked to argue both sides writes the second half already
    knowing what it said in the first. That anchoring is the thing separate
    contexts exist to prevent."""
    fake = _script("BULL SAYS", "BEAR SAYS", "BRIEFING")
    monkeypatch.setattr(agent, "stream_turn", fake)

    debate.review("NVDA")

    bull_prompt = fake.calls[0]["messages"][0]["content"]
    bear_prompt = fake.calls[1]["messages"][0]["content"]

    assert len(fake.calls[0]["messages"]) == 1, "the bull starts from nothing"
    assert len(fake.calls[1]["messages"]) == 1, "so does the bear"
    assert "BULL SAYS" not in bear_prompt, "the bear never sees the bull's work"
    assert "case FOR" in bull_prompt and "case AGAINST" in bear_prompt


def test_the_synthesis_sees_both_and_neither_side_sees_it(monkeypatch):
    fake = _script("BULL SAYS", "BEAR SAYS", "BRIEFING")
    monkeypatch.setattr(agent, "stream_turn", fake)

    result = debate.review("NVDA")

    synthesis = fake.calls[2]["messages"][0]["content"]
    assert "BULL SAYS" in synthesis and "BEAR SAYS" in synthesis
    assert result["briefing"] == "BRIEFING"
    assert result["bull"] == "BULL SAYS" and result["bear"] == "BEAR SAYS"


# --- what it may touch ------------------------------------------------------


def test_research_is_read_only_and_nothing_it_may_call_is_gated():
    """A debate about whether to hold a stock has no business being able to
    email anyone."""
    gated = {k for k, v in settings.DEFAULTS["autonomy"]["tools"].items() if v == settings.CONFIRM}

    assert not set(debate.RESEARCH_TOOLS) & gated

    from ada.tools.registry import get as get_tool

    for name in debate.RESEARCH_TOOLS:
        tool = get_tool(name)
        assert tool is not None, f"{name} isn't a real tool"
        assert tool.confirm_when is None, f"{name} can ask for confirmation"


def test_the_sides_are_handed_only_the_research_tools(monkeypatch):
    fake = _script("a", "b", "c")
    monkeypatch.setattr(agent, "stream_turn", fake)

    debate.review("NVDA")

    for call in fake.calls[:2]:
        assert {t["name"] for t in call["tools"]} == set(debate.RESEARCH_TOOLS)


def test_the_synthesis_gets_no_tools_at_all(monkeypatch):
    """It has both cases in front of it. A turn that can search will search,
    spending money to fetch a headline it was already handed."""
    fake = _script("a", "b", "c")
    monkeypatch.setattr(agent, "stream_turn", fake)

    debate.review("NVDA")

    assert fake.calls[2]["tools"] == []


def test_a_gate_reached_during_research_fails_loudly(monkeypatch):
    """It should be unreachable. If it happens, the allowlist and the autonomy
    config have drifted, and silently approving or declining would hide it."""
    with pytest.raises(RuntimeError, match="drifted"):
        debate._no_confirmations(Action(tool="send_message", input={}, description="x"))


def test_a_research_turn_that_hits_the_gate_stops_rather_than_proceeding(monkeypatch):
    """End to end through run_turn: if a research tool ever became gated, the
    review stops instead of quietly approving or quietly declining."""
    monkeypatch.setattr(safety, "requires_confirmation", lambda name: True)

    def wants_a_tool(messages, tools):
        message = FakeMessage(
            [FakeBlock("tool_use", id="t1", name="check_price", input={"symbol": "NVDA"})], "tool_use"
        )

        def generator():
            return message
            yield  # pragma: no cover — marks this a generator

        return generator()

    monkeypatch.setattr(agent, "stream_turn", wants_a_tool)

    with pytest.raises(RuntimeError, match="drifted"):
        debate.review("NVDA")


# --- the holder's own words -------------------------------------------------


def test_what_they_wrote_down_reaches_the_briefing(monkeypatch):
    """The whole point. Without it this is a market opinion, which is worth
    very little."""
    positions.add(
        "NVDA", 12, cost=180, stop=165,
        thesis="datacentre demand keeps compounding",
        invalidation="if hyperscaler capex guidance turns down two quarters running",
    )
    fake = _script("a", "b", "c")
    monkeypatch.setattr(agent, "stream_turn", fake)

    debate.review("NVDA")

    synthesis = fake.calls[2]["messages"][0]["content"]
    assert "hyperscaler capex guidance" in synthesis
    assert "datacentre demand" in synthesis
    assert "165" in synthesis


def test_a_missing_invalidation_is_named_as_the_gap(monkeypatch):
    positions.add("NVDA", 12, cost=180)
    fake = _script("a", "b", "c")
    monkeypatch.setattr(agent, "stream_turn", fake)

    debate.review("NVDA")

    assert "never wrote down what would make them wrong" in fake.calls[2]["messages"][0]["content"]


def test_reviewing_something_not_held_says_so(monkeypatch):
    fake = _script("a", "b", "c")
    monkeypatch.setattr(agent, "stream_turn", fake)

    debate.review("TSLA")

    assert "don't currently hold TSLA" in fake.calls[2]["messages"][0]["content"]


# --- what it refuses to be --------------------------------------------------


def test_the_synthesis_is_told_not_to_give_a_verdict(monkeypatch):
    """A verdict that changes between runs is noise wearing a suit."""
    fake = _script("a", "b", "c")
    monkeypatch.setattr(agent, "stream_turn", fake)

    debate.review("NVDA")
    synthesis = fake.calls[2]["messages"][0]["content"]

    assert "Do NOT recommend buying, selling or holding" in synthesis
    assert "Do not give a rating, a score, a price target or a probability" in synthesis


def test_the_reply_says_what_it_cost_and_that_it_is_not_advice(monkeypatch):
    from ada.tools import positions_tool

    monkeypatch.setattr(debate, "review", lambda s: {
        "symbol": s, "bull": "B", "bear": "R", "briefing": "BRIEF", "spent": 0.1234,
    })

    out = positions_tool.review_holding("NVDA")

    assert "BRIEF" in out
    assert "0.1234" in out
    assert "not advice" in out
    assert "the case for" not in out, "the workings stay hidden unless asked for"


def test_the_workings_are_available_when_asked(monkeypatch):
    from ada.tools import positions_tool

    monkeypatch.setattr(debate, "review", lambda s: {
        "symbol": s, "bull": "BULLWORK", "bear": "BEARWORK", "briefing": "BRIEF", "spent": 0.1,
    })

    out = positions_tool.review_holding("NVDA", full=True)

    assert "BULLWORK" in out and "BEARWORK" in out


def test_a_review_needs_a_symbol():
    with pytest.raises(ValueError, match="Which holding"):
        debate.review("  ")
