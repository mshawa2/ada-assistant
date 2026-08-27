"""The analyst team, the scan, and the outside framework kept outside.

The invariants: each specialist works in its own context, the risk manager sees
the user's real account and gets the last word, the expensive tools can't be
reached by accident, and nothing anywhere can place an order.
"""

import pytest

from ada import analyst, budget, debate, positions
from tests.conftest import FakeBlock, FakeMessage


def _script():
    calls = []

    def stream_turn(messages, tools):
        calls.append({"prompt": messages[0]["content"], "tools": tools})
        message = FakeMessage([FakeBlock("text", text=f"REPLY {len(calls)}")], "end_turn")

        def generator():
            yield message.content[0].text
            return message

        return generator()

    stream_turn.calls = calls
    return stream_turn


@pytest.fixture
def scripted(monkeypatch):
    from ada import agent

    fake = _script()
    monkeypatch.setattr(agent, "stream_turn", fake)
    return fake


# --- the team ---------------------------------------------------------------


def test_every_specialist_works_in_its_own_context(scripted):
    """An analyst that can see another's report writes a response to it rather
    than an independent read."""
    analyst.analyze("NVDA")

    prompts = [c["prompt"] for c in scripted.calls]
    news, fundamentals, sentiment, technical = prompts[:4]

    assert "news and macro analyst" in news
    assert "fundamentals analyst" in fundamentals
    assert "sentiment analyst" in sentiment
    assert "technical analyst" in technical
    for prompt in prompts[:4]:
        assert "REPLY" not in prompt, "no specialist sees another's output"


def test_the_analysts_research_and_everyone_after_them_reads(scripted):
    """Once four reports exist, a turn that can search will search — paying to
    fetch what it was already handed."""
    analyst.analyze("NVDA")

    for call in scripted.calls[:4]:
        assert {t["name"] for t in call["tools"]} == set(analyst.RESEARCH_TOOLS)
    for call in scripted.calls[4:]:
        assert call["tools"] == [], "debate, trader and risk read rather than research"


def test_the_debate_and_the_trader_see_the_reports(scripted):
    analyst.analyze("NVDA")

    bull, bear, trader = scripted.calls[4]["prompt"], scripted.calls[5]["prompt"], scripted.calls[6]["prompt"]

    assert "NEWS ANALYST" in bull and "NEWS ANALYST" in bear
    assert "REPLY 5" in trader and "REPLY 6" in trader, "the trader sees both sides"
    assert "REPLY 5" not in bear, "the bear never sees the bull"


def test_the_trader_is_asked_to_actually_commit(scripted):
    """A trader who won't commit is no use, and hedging into uselessness is
    its own kind of dishonesty."""
    analyst.analyze("NVDA")
    trader = scripted.calls[6]["prompt"]

    assert "Make a call" in trader
    assert "Conviction" in trader
    assert "What would change your mind" in trader
    assert "where you'd be wrong fastest" in trader.lower()


# --- risk gets the last word ------------------------------------------------


def test_risk_runs_last_and_can_veto(scripted):
    analyst.analyze("NVDA")
    risk = scripted.calls[-1]["prompt"]

    assert "risk manager" in risk
    assert "last word" in risk
    assert "veto" in risk
    assert "REPLY 7" in risk, "it sees the trader's call"


def test_risk_sees_what_the_user_actually_holds(scripted):
    """The thing generic advice cannot do."""
    positions.add("AAPL", 20, cost=300, stop=290)
    positions.record_reading("AAPL", 310)

    analyst.analyze("NVDA")
    risk = scripted.calls[-1]["prompt"]

    assert "20 AAPL" in risk
    assert "stop 290.00" in risk
    assert "6200" in risk, "and roughly what it's worth"


def test_risk_is_told_not_to_invent_an_account_it_cannot_see(scripted):
    analyst.analyze("NVDA")
    risk = scripted.calls[-1]["prompt"]

    assert "hold nothing" in risk
    assert "do not invent one" in risk.lower()


def test_risk_must_produce_the_sentence_ada_will_hold_them_to(scripted):
    """It closes the loop with the positions feature: the invalidation written
    before buying is what gets handed back when an alert fires."""
    analyst.analyze("NVDA")

    assert "Invalidation" in scripted.calls[-1]["prompt"]


# --- the scan ---------------------------------------------------------------


def test_the_scan_is_one_turn_and_researches(scripted):
    analyst.scan()

    assert len(scripted.calls) == 1
    assert {t["name"] for t in scripted.calls[0]["tools"]} == set(analyst.RESEARCH_TOOLS)


def test_the_scan_refuses_to_pad_the_list(scripted):
    analyst.scan()
    prompt = scripted.calls[0]["prompt"]

    assert "Do not include a name whose catalyst you could not actually find" in prompt
    assert "three real ones beat five" in prompt


def test_the_scan_knows_what_they_already_hold(scripted):
    positions.add("NVDA", 10)

    analyst.scan()

    assert "already holds: NVDA" in scripted.calls[0]["prompt"]


def test_a_ticker_is_required():
    with pytest.raises(ValueError, match="Which ticker"):
        analyst.analyze("  ")



# --- the line that doesn't move ---------------------------------------------


def test_nothing_added_here_can_place_an_order():
    from ada.tools import specs

    for spec in specs():
        assert not spec["name"].startswith(("buy_", "sell_", "trade_", "order_", "execute_"))


def test_the_expensive_tools_say_what_they_cost():
    from ada.tools import specs

    priced = {s["name"]: s["description"] for s in specs()}

    assert "$1.00" in priced["analyze_ticker"]
    assert "$0.20" in priced["market_scan"]
    assert "check_price is instant and free" in priced["analyze_ticker"]


def test_the_tool_registry_can_be_imported_before_the_agent():
    """`ada.agent` imports the tool registry, and the registry imports tools
    that import `debate`. A module-level `from ada import agent` in debate.py
    made `import ada.tools` fail on a half-built module whenever nothing had
    imported the agent first — which a test doing exactly that found."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "from ada.tools import specs; print(len(specs()))"],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "ANTHROPIC_API_KEY": "test"},
    )

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0
