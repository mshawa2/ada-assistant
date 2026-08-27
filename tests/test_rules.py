"""Choosing a stop: the levels a holding has, and what each one costs.

The module exists because of one observation — above your cost, a stop protects
a gain rather than capping a loss, and the number that decides it is what the
level *keeps*, not how far below the price it sits. Everything here is
arithmetic, so it can be instant and free.
"""

import pytest

from ada import positions, rules


def _reading(price, **extra):
    return {"symbol": "GE", "currency": "USD", "price": price, **extra}


def _held(shares=10, cost=100, **fields):
    return positions.add("GE", shares, cost=cost, **fields)


# --- what a level would bank ------------------------------------------------


def test_a_level_says_what_it_would_bank():
    """The whole point. "20% below" means nothing next to "banks $1,187"."""
    _held(shares=10, cost=100)

    found = {level["name"]: level for level in rules.levels(positions.get("GE"), _reading(200))}

    assert found["10% below"]["price"] == pytest.approx(180)
    assert found["10% below"]["banks"] == pytest.approx((180 - 100) * 10)
    assert found["20% below"]["banks"] == pytest.approx((160 - 100) * 10)


def test_a_level_above_the_price_is_flagged_as_firing_immediately():
    """Worth knowing before it's set, rather than from the notification a
    second later."""
    _held(cost=100)

    found = {l["name"]: l for l in rules.levels(positions.get("GE"), _reading(200, sma50=210))}

    assert found["50-day average"]["breached"] is True
    assert found["10% below"]["breached"] is False


def test_a_stop_at_cost_banks_nothing_and_says_so():
    _held(shares=10, cost=100)

    at_cost = next(l for l in rules.levels(positions.get("GE"), _reading(200)) if l["name"] == "your cost")

    assert at_cost["banks"] == pytest.approx(0)


def test_a_holding_with_no_cost_still_gets_levels():
    positions.add("GE", 10)

    found = rules.levels(positions.get("GE"), _reading(200))

    assert found, "levels don't depend on knowing the cost"
    assert all(level["banks"] is None for level in found)


def test_levels_are_ordered_nearest_first():
    """That's the order someone reads them in; the far ones are rarely it."""
    _held(cost=100)

    prices = [l["price"] for l in rules.levels(positions.get("GE"), _reading(200, sma50=190, sma200=150))]

    assert prices == sorted(prices, reverse=True)


def test_the_levels_a_holding_actually_has_are_used():
    """Its own moving averages and 52-week low, not just round percentages."""
    _held(cost=100)

    names = {l["name"] for l in rules.levels(positions.get("GE"), _reading(200, sma50=190, sma200=150, year_low=120))}

    assert {"50-day average", "200-day average", "52-week low", "your cost"} <= names


# --- what kind of situation it is -------------------------------------------


def test_a_winner_and_a_loser_are_different_situations():
    _held(shares=10, cost=100)

    up = rules.shape(positions.get("GE"), _reading(200))
    assert up["winner"] is True and up["underwater"] is False
    assert up["gain"] == pytest.approx(1000)

    down = rules.shape(positions.get("GE"), _reading(80))
    assert down["underwater"] is True and down["winner"] is False


def test_a_small_gain_is_not_yet_a_winner():
    """Below the threshold a stop is still mostly about the downside."""
    _held(cost=100)

    assert rules.shape(positions.get("GE"), _reading(105))["winner"] is False


# --- how it reads -----------------------------------------------------------


def test_a_winner_is_told_the_stop_protects_a_gain():
    _held(shares=10, cost=100)

    text = rules.describe(positions.get("GE"), _reading(200))

    assert "protects a gain rather than capping a loss" in text
    assert "banks" in text


def test_an_underwater_holding_is_told_the_opposite():
    _held(cost=100)

    text = rules.describe(positions.get("GE"), _reading(80))

    assert "below cost, so a stop caps a loss" in text
    assert "fire immediately" in text


def test_a_trailing_option_is_offered_only_to_winners():
    _held(shares=10, cost=100)
    positions.record_reading("GE", 200)

    assert "trail" in rules.describe(positions.get("GE"), _reading(200))
    assert "trail" not in rules.describe(positions.get("GE"), _reading(101))


def test_a_trail_is_measured_from_the_high_not_the_price():
    _held(cost=100)
    positions.record_reading("GE", 200)
    positions.record_reading("GE", 150)

    assert rules.trail_price(positions.get("GE"), 10) == pytest.approx(180), "from the 200 high"


def test_nothing_here_tells_anyone_what_to_do():
    """The holder picks; this makes the picking informed. The moment it says
    'recommended' it has become a tipster."""
    _held(shares=10, cost=100)

    text = rules.describe(positions.get("GE"), _reading(200, rsi14=75)).lower()

    for word in ("recommend", "you should", "i suggest", "best level", "advise"):
        assert word not in text, f"reads as advice: {word}"


def test_an_unruled_holding_is_told_it_will_never_alert():
    _held(cost=100)

    assert "never alert" in rules.describe(positions.get("GE"), _reading(200))


# --- where to start ---------------------------------------------------------


def test_unguarded_holdings_are_ranked_by_money_at_stake():
    """A list of names doesn't say which to do first."""
    for symbol, shares, price in (("SMALL", 1, 10), ("BIG", 100, 50), ("MID", 10, 100)):
        positions.merge_from_broker([{"symbol": symbol, "shares": shares, "cost": price / 2}])
        positions.record_reading(symbol, price)

    assert [h["symbol"] for h in rules.unguarded()] == ["BIG", "MID", "SMALL"]
    assert rules.unguarded()[0]["value"] == pytest.approx(5000)


def test_a_holding_with_a_rule_is_not_unguarded():
    positions.merge_from_broker([{"symbol": "GE", "shares": 10, "cost": 100}])
    positions.record_reading("GE", 200)
    positions.update("GE", stop=150)

    assert rules.unguarded() == []


def test_a_holding_with_no_price_is_left_out_rather_than_valued_at_zero():
    positions.merge_from_broker([{"symbol": "GE", "shares": 10, "cost": 100}])

    assert rules.unguarded() == []


def test_the_tool_leads_with_the_total_exposed():
    from ada.tools import positions_tool

    positions.merge_from_broker([{"symbol": "BIG", "shares": 100, "cost": 25}])
    positions.record_reading("BIG", 50)

    text = positions_tool.unguarded_holdings()

    assert "5,000" in text
    assert "never alert" in text


def test_everything_ruled_says_so():
    from ada.tools import positions_tool

    assert "Every holding" in positions_tool.unguarded_holdings()
