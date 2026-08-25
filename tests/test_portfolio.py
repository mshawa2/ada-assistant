"""Syncing a real book into Ada's, without trampling what you decided.

The merge table is the feature. Every row of it gets a test, and the two that
matter most are "your rules survive a sync" and "a position the broker stops
reporting is reported, not deleted".
"""

import pytest

from ada import brokerage, portfolio, positions


def _row(symbol, shares, cost=None, account="acct-1"):
    return {"symbol": symbol, "shares": shares, "cost": cost, "account_id": account}


# --- the merge table --------------------------------------------------------


def test_your_rules_survive_a_sync():
    """The whole reason merge_from_broker exists. add() would replace the
    record and erase every one of these."""
    positions.add(
        "NVDA", 10, cost=180, stop=165, target=240, trail_pct=12,
        thesis="datacentre buildout", invalidation="two quarters of capex cuts",
    )
    positions.record_reading("NVDA", 220)
    positions.set_fired("NVDA", "target", True)

    positions.merge_from_broker([_row("NVDA", 12, 190)])

    after = positions.get("NVDA")
    assert after["stop"] == 165
    assert after["target"] == 240
    assert after["trail_pct"] == 12
    assert after["thesis"] == "datacentre buildout"
    assert after["invalidation"] == "two quarters of capex cuts"
    assert after["high_water"] == 220, "Ada's bookkeeping survives too"
    assert after["fired"] == {"target": True}


def test_the_broker_owns_shares_and_cost():
    positions.add("NVDA", 10, cost=180, stop=165)

    positions.merge_from_broker([_row("NVDA", 12, 190)])

    after = positions.get("NVDA")
    assert after["shares"] == 12
    assert after["cost"] == 190
    assert after["source"] == "snaptrade"
    assert after["account_id"] == "acct-1"


def test_something_new_arrives_with_no_rules_and_is_named_as_such():
    result = positions.merge_from_broker([_row("AAPL", 20, 300)])

    assert result["added"] == ["AAPL"]
    added = positions.get("AAPL")
    assert added["stop"] is None
    assert added["invalidation"] == ""
    assert added["shares"] == 20


def test_a_holding_the_broker_stops_reporting_is_never_deleted():
    """A sync that hiccups and drops a position carrying a stop removes an
    alarm without telling anyone. That is the worst failure available here."""
    positions.merge_from_broker([_row("NVDA", 10, 180)])
    positions.update("NVDA", stop=165)

    result = positions.merge_from_broker([])

    assert result["missing"] == ["NVDA"]
    assert positions.get("NVDA") is not None, "still tracked"
    assert positions.get("NVDA")["stop"] == 165, "still alarmed"


def test_what_you_typed_in_yourself_is_never_reported_missing():
    """It was never the broker's to report."""
    positions.add("TSLA", 5, cost=200)

    result = positions.merge_from_broker([_row("NVDA", 10, 180)])

    assert "TSLA" not in result["missing"]
    assert positions.get("TSLA")["source"] == "manual"


def test_only_a_real_change_counts_as_updated():
    positions.merge_from_broker([_row("NVDA", 10, 180)])

    assert positions.merge_from_broker([_row("NVDA", 10, 180)])["updated"] == []
    assert positions.merge_from_broker([_row("NVDA", 11, 180)])["updated"] == ["NVDA"]


def test_the_same_symbol_in_two_accounts_is_one_position():
    """Ada tracks a symbol, not a lot — so shares add and cost is
    share-weighted."""
    positions.merge_from_broker(
        [_row("NVDA", 10, 100, "a"), _row("NVDA", 10, 200, "b")]
    )

    held = positions.get("NVDA")
    assert held["shares"] == 20
    assert held["cost"] == pytest.approx(150)
    assert held["account_id"] == "multiple"


def test_rows_without_a_symbol_are_skipped_not_guessed():
    """A position keyed by the wrong string would look like a new holding
    forever."""
    positions.merge_from_broker([{"shares": 5}, _row("NVDA", 10, 180)])

    assert [p["symbol"] for p in positions.load_positions()] == ["NVDA"]


# --- reading the broker's shapes --------------------------------------------


@pytest.mark.parametrize(
    "holding,expected",
    [
        ({"symbol": "nvda"}, "NVDA"),
        ({"symbol": {"symbol": "AAPL"}}, "AAPL"),
        ({"symbol": {"raw_symbol": "TSLA"}}, "TSLA"),
        ({"symbol": {"symbol": {"raw_symbol": "MSFT"}}}, "MSFT"),
        ({"symbol": {}}, None),
        ({}, None),
    ],
)
def test_the_ticker_is_found_in_whichever_shape_the_broker_used(holding, expected):
    assert portfolio._symbol_of(holding) == expected


def test_units_and_cost_are_read_from_any_of_their_names():
    assert portfolio._units({"units": "3"}) == 3
    assert portfolio._units({"quantity": 4}) == 4
    assert portfolio._units({}) == 0
    assert portfolio._cost({"average_purchase_price": "180.5"}) == 180.5
    assert portfolio._cost({"averagePurchasePrice": 200}) == 200
    assert portfolio._cost({}) is None


def test_cash_sums_across_currencies():
    cash, power = portfolio._cash([{"cash": 100, "buying_power": 200}, {"cash": 50}])

    assert cash == 150
    assert power == 200


# --- the sync as a whole ----------------------------------------------------


@pytest.fixture
def broker(monkeypatch):
    state = {
        "accounts": [{"id": "acct-1", "name": "Robinhood Individual", "number": "123456789012"}],
        "holdings": {
            "balances": [{"cash": 2500.0, "buying_power": 5000.0}],
            "positions": [
                {"symbol": {"symbol": "NVDA"}, "units": 12, "average_purchase_price": 190.0},
                {"symbol": {"symbol": "AAPL"}, "units": 20, "average_purchase_price": 300.0},
            ],
        },
    }
    monkeypatch.setattr(brokerage, "accounts", lambda: state["accounts"])
    monkeypatch.setattr(brokerage, "holdings", lambda account_id: state["holdings"])
    monkeypatch.setattr(brokerage, "balances", lambda account_id: state["holdings"]["balances"])
    return state


def test_a_sync_reads_the_book_and_merges_it(broker):
    result = portfolio.sync()

    assert result["added"] == ["AAPL", "NVDA"]
    assert {p["symbol"] for p in positions.load_positions()} == {"AAPL", "NVDA"}
    assert positions.get("NVDA")["shares"] == 12


def test_a_sync_records_cash_and_masks_the_account_number(broker):
    result = portfolio.sync()
    account = result["accounts"][0]

    assert account["cash"] == 2500.0
    assert account["buying_power"] == 5000.0
    assert account["number"] == "••••9012", "a full account number never leaves brokerage.py"


def test_the_dashboard_reads_the_last_sync_rather_than_calling_out(broker):
    portfolio.sync()

    stored = portfolio.last_sync()

    assert stored["accounts"][0]["id"] == "acct-1"
    assert stored["at"] is not None


def test_nothing_synced_yet_is_an_empty_state_not_a_crash():
    assert portfolio.last_sync() == {"accounts": [], "at": None}


def test_a_sync_with_no_connection_raises_rather_than_emptying_your_book(monkeypatch):
    positions.add("NVDA", 10, cost=180, stop=165)
    monkeypatch.delenv(brokerage.CLIENT_ID_VAR, raising=False)
    monkeypatch.delenv(brokerage.CONSUMER_KEY_VAR, raising=False)

    with pytest.raises(brokerage.NotConnected):
        portfolio.sync()

    assert positions.get("NVDA")["stop"] == 165, "an outage changes nothing"


# --- how it reads back ------------------------------------------------------


def test_new_holdings_are_reported_as_inert_until_you_set_rules(broker):
    text = portfolio.describe(portfolio.sync())

    assert "New to Ada: AAPL, NVDA" in text
    assert "never alert" in text, "the whole point of saying so"


def test_a_missing_holding_is_reported_as_a_question_not_a_deletion(broker):
    portfolio.sync()
    broker["holdings"]["positions"] = []

    text = portfolio.describe(portfolio.sync())

    assert "no longer reports" in text
    assert "Still tracked" in text


def test_a_quiet_sync_says_so(broker):
    portfolio.sync()

    assert "Nothing changed" in portfolio.describe(portfolio.sync())
