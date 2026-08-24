"""Holdings, and the rules that alert on them.

Every trigger here is arithmetic against a number the user chose. The tests
that matter most are the ones pinning that down — that an indicator never fires
an alert, that an alert fires once, and that nothing in this feature can trade.
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ada import market, positions
from ada.heartbeat import notices, positions_check
from ada.tools import positions_tool


def _reading(price, **extra):
    return {"symbol": "NVDA", "currency": "USD", "price": price, **extra}


def _held(**fields):
    return positions.add("NVDA", 10, cost=180, stop=165, target=240, **fields)


# --- the indicators ---------------------------------------------------------


def test_a_moving_average_needs_enough_history():
    """A "200-day average" from forty days is a different number wearing the
    same label, and quietly wrong exactly where it matters."""
    assert market.sma([1, 2, 3, 4, 5], 5) == 3
    assert market.sma([1, 2], 5) is None


def test_rsi_pins_at_the_ends_rather_than_dividing_by_zero():
    assert market.rsi([float(i) for i in range(1, 20)]) == 100.0
    assert market.rsi([float(i) for i in range(20, 1, -1)]) == 0.0
    assert market.rsi([5.0] * 20) == 50.0


def test_rsi_needs_more_than_its_window():
    assert market.rsi([1.0] * 14, 14) is None


def test_a_percentage_of_nothing_is_nothing():
    assert market.pct(10, 0) is None
    assert market.pct(None, 10) is None
    assert market.pct(110, 100) == pytest.approx(10)


# --- the rules --------------------------------------------------------------


def test_a_stop_fires_at_or_below_the_level_you_set():
    held = _held()

    assert [a["kind"] for a in positions.evaluate(held, _reading(164))] == ["stop"]
    assert [a["kind"] for a in positions.evaluate(held, _reading(165))] == ["stop"], "at the level counts"
    assert positions.evaluate(held, _reading(170)) == []


def test_a_target_fires_at_or_above_the_level_you_set():
    held = _held()

    assert [a["kind"] for a in positions.evaluate(held, _reading(241))] == ["target"]
    assert positions.evaluate(held, _reading(239)) == []


def test_a_trailing_alert_measures_from_the_high_not_the_cost():
    held = _held(trail_pct=10)
    positions.record_reading("NVDA", 200)

    held = positions.get("NVDA")
    assert positions.evaluate(held, _reading(185)) == [], "7.5% down, inside the 10%"

    found = positions.evaluate(held, _reading(179))
    assert [a["kind"] for a in found] == ["trail"]
    assert "10%" in found[0]["text"]


def test_the_high_water_mark_only_ever_rises():
    """It's what makes a trailing stop trailing — tracking the price down, it
    would never fire."""
    _held()
    positions.record_reading("NVDA", 200)
    positions.record_reading("NVDA", 150)

    assert positions.get("NVDA")["high_water"] == 200


def test_earnings_warn_before_they_happen_not_after():
    soon = time.time() + 2 * 86400
    held = _held(earnings_on=soon)

    assert [a["kind"] for a in positions.evaluate(held, _reading(200))] == ["earnings"]

    past = positions.add("NVDA", 10, cost=180, earnings_on=time.time() - 86400)
    assert positions.evaluate(past, _reading(200)) == []


def test_an_indicator_never_fires_an_alert():
    """The line this whole feature stands on. RSI predicts nothing, so it
    raises nothing."""
    held = positions.add("NVDA", 10, cost=180)

    found = positions.evaluate(held, _reading(200, rsi14=95, above_sma50_pct=60))

    assert found == []


def test_indicators_come_back_as_context_worded_as_context():
    held = positions.add("NVDA", 10)

    note = positions.context(held, _reading(200, rsi14=78, above_sma50_pct=14))

    assert "RSI 78" in note
    assert "+14%" in note
    for verb in ("sell", "buy", "overbought", "should", "consider"):
        assert verb not in note.lower(), f"context must not read as advice: {note}"


# --- alerting once ----------------------------------------------------------


def test_an_alert_fires_once_and_re_arms_when_it_clears(monkeypatch, sent_notifications):
    """A stop that's been hit is still hit tomorrow, and re-sending it every
    fifteen minutes is how someone learns to ignore the one that mattered."""
    _held()
    prices = iter([160, 160, 170, 160])
    monkeypatch.setattr(market, "read", lambda s: _reading(next(prices)))
    monkeypatch.setattr(positions_check, "market_open", lambda now=None: True)

    positions_check.check_positions({})
    assert len(notices.list_notices()) == 1

    positions_check.check_positions({})
    assert len(notices.list_notices()) == 1, "still hit, not news again"

    positions_check.check_positions({})  # recovers above the stop
    assert positions.get("NVDA")["fired"].get("stop") is None

    positions_check.check_positions({})  # falls back through it
    assert len(notices.list_notices()) == 2, "re-armed, so it speaks again"


def test_a_stop_being_hit_is_the_one_thing_worth_interrupting_for(monkeypatch, sent_notifications):
    _held()
    monkeypatch.setattr(market, "read", lambda s: _reading(160))
    monkeypatch.setattr(positions_check, "market_open", lambda now=None: True)

    positions_check.check_positions({})

    assert notices.list_notices()[0]["level"] == "critical"


def test_the_alert_hands_back_what_you_said_would_make_you_wrong(monkeypatch, sent_notifications):
    """The point of the feature: the sentence written while calm, arriving at
    the moment nobody is."""
    _held(invalidation="if it closes below the 50-day on heavy volume")
    monkeypatch.setattr(market, "read", lambda s: _reading(160))
    monkeypatch.setattr(positions_check, "market_open", lambda now=None: True)

    positions_check.check_positions({})

    assert "closes below the 50-day" in notices.list_notices()[0]["text"]


def test_moving_a_stop_re_arms_it(monkeypatch, sent_notifications):
    _held()
    monkeypatch.setattr(market, "read", lambda s: _reading(160))
    monkeypatch.setattr(positions_check, "market_open", lambda now=None: True)
    positions_check.check_positions({})

    positions.update("NVDA", stop=170)

    assert positions.get("NVDA")["fired"].get("stop") is None
    positions_check.check_positions({})
    assert len(notices.list_notices()) == 2


def test_a_price_that_cannot_be_read_is_not_silence(monkeypatch, sent_notifications, capsys):
    """"Don't know" and "nothing to report" are different, and a flaky
    endpoint must not look like a stop being hit — or like calm."""
    _held()

    def broken(symbol):
        raise market.MarketError("the price service returned 503")

    monkeypatch.setattr(market, "read", broken)
    monkeypatch.setattr(positions_check, "market_open", lambda now=None: True)

    positions_check.check_positions({})

    assert notices.list_notices() == [], "no alert invented from a missing price"
    assert "couldn't price NVDA" in capsys.readouterr().out


# --- market hours -----------------------------------------------------------


@pytest.mark.parametrize(
    "moment,expected",
    [
        ((2026, 8, 24, 10, 0), True),
        ((2026, 8, 24, 9, 29), False),
        ((2026, 8, 24, 16, 0), False),
        ((2026, 8, 24, 3, 0), False),
        ((2026, 8, 22, 11, 0), False),
        ((2026, 8, 23, 11, 0), False),
    ],
)
def test_the_clock_and_the_weekend(moment, expected):
    at = datetime(*moment, tzinfo=ZoneInfo("America/New_York")).timestamp()

    assert positions_check.market_open(at) is expected


def test_nothing_is_checked_when_the_market_is_shut(monkeypatch, sent_notifications):
    """Prices don't move at 3am, and re-alerting on a stale close is worse
    than saying nothing."""
    _held()
    called = []
    monkeypatch.setattr(market, "read", lambda s: called.append(s) or _reading(160))
    monkeypatch.setattr(positions_check, "market_open", lambda now=None: False)

    positions_check.check_positions({})

    assert called == []


# --- the tools --------------------------------------------------------------


def test_recording_a_holding_asks_for_what_is_missing(monkeypatch):
    monkeypatch.setattr(market, "read", lambda s: _reading(190))

    result = positions_tool.add_position("NVDA", 10, cost=180)

    assert "10 NVDA" in result
    assert "a stop" in result and "make them wrong" in result


def test_recording_a_holding_with_its_rules_asks_for_nothing(monkeypatch):
    monkeypatch.setattr(market, "read", lambda s: _reading(190))

    result = positions_tool.add_position(
        "NVDA", 10, cost=180, stop=165, invalidation="if the datacentre story breaks"
    )

    assert "worth asking" not in result


def test_a_holding_needs_a_symbol_and_real_shares():
    with pytest.raises(ValueError, match="needs a symbol"):
        positions.add("", 10)
    with pytest.raises(ValueError, match="positive number of shares"):
        positions.add("NVDA", 0)


def test_removing_a_holding_says_plainly_that_nothing_was_sold():
    _held()

    assert "Ada can't" in positions_tool.remove_position("NVDA")
    assert positions.load_positions() == []


def test_the_digest_shows_each_holding_against_its_own_rules(monkeypatch):
    _held(invalidation="if the story breaks")
    monkeypatch.setattr(market, "read", lambda s: _reading(160, rsi14=25))

    text = positions_check.digest()

    assert "NVDA 10 @ 160.00" in text
    assert "-11.1%" in text, "profit and loss against cost"
    assert "stop 165.00" in text
    assert "at or below the 165.00 stop" in text
    assert "if the story breaks" in text


def test_the_digest_says_so_when_it_has_nothing():
    assert "not holding anything" in positions_check.digest()


def test_no_tool_in_ada_can_place_an_order():
    """Not an unfinished edge — an agent that can act on a price alert
    unattended is a different and much worse product."""
    from ada.tools import specs

    for spec in specs():
        blob = (spec["name"] + " " + spec["description"]).lower()
        assert "place an order" not in blob
        assert not spec["name"].startswith(("buy_", "sell_", "trade_", "order_"))


def test_the_position_tools_are_registered():
    from ada.tools import specs

    names = {s["name"] for s in specs()}
    assert {"add_position", "list_positions", "update_position", "remove_position", "check_price"} <= names


# --- the brokerage seam -----------------------------------------------------


def test_an_absent_connection_never_looks_like_an_empty_portfolio(monkeypatch):
    """The same shape, opposite meanings — confusing them is how someone
    concludes they sold something."""
    from ada import brokerage

    monkeypatch.delenv(brokerage.CLIENT_ID_VAR, raising=False)
    monkeypatch.delenv(brokerage.CONSUMER_KEY_VAR, raising=False)

    assert brokerage.configured() is False
    with pytest.raises(brokerage.NotConnected):
        brokerage.positions()


def test_the_setup_help_says_what_to_do_next(monkeypatch):
    from ada import brokerage

    monkeypatch.delenv(brokerage.CLIENT_ID_VAR, raising=False)
    help_text = brokerage.setup_help()

    assert "snaptrade.com" in help_text
    assert "never sees a password" in help_text
    assert "tell ada what you hold" in help_text.lower(), "it says what still works meanwhile"
