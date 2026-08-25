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


# --- the tools --------------------------------------------------------------


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(brokerage.CLIENT_ID_VAR, "test-client")
    monkeypatch.setenv(brokerage.CONSUMER_KEY_VAR, "test-key")


def test_no_portfolio_tool_can_move_money():
    """The moment after a sync is exactly when someone asks 'so can you just
    sell it then'."""
    from ada.tools import specs

    for spec in specs():
        assert not spec["name"].startswith(("buy_", "sell_", "trade_", "order_", "place_"))

    sync = next(s for s in specs() if s["name"] == "sync_portfolio")
    assert "only ever reads" in sync["description"]
    assert "cannot place, cancel or change an order" in sync["description"]


def test_an_unconfigured_sync_explains_setup_rather_than_reporting_nothing(monkeypatch):
    from ada.tools import portfolio_tool

    monkeypatch.delenv(brokerage.CLIENT_ID_VAR, raising=False)
    monkeypatch.delenv(brokerage.CONSUMER_KEY_VAR, raising=False)

    out = portfolio_tool.sync_portfolio()

    assert "snaptrade.com" in out
    assert "tell ada what you hold" in out.lower()


def test_a_brokerage_outage_says_nothing_was_changed(monkeypatch, keyed):
    from ada.tools import portfolio_tool

    def broken():
        raise brokerage.BrokerageError("SnapTrade returned 503.")

    monkeypatch.setattr(brokerage, "accounts", broken)

    out = portfolio_tool.sync_portfolio()

    assert "nothing was changed" in out
    assert "503" in out


def test_listing_accounts_reads_the_last_sync(broker, keyed):
    from ada.tools import portfolio_tool

    portfolio.sync()
    out = portfolio_tool.list_accounts()

    assert "Robinhood Individual" in out
    assert "••••9012" in out
    assert "cash 2,500.00" in out
    assert "2 holdings" in out


def test_listing_accounts_before_any_sync_says_what_to_do(keyed):
    from ada.tools import portfolio_tool

    assert "sync_portfolio" in portfolio_tool.list_accounts()


def test_holdings_without_rules_are_named_as_inert():
    from ada.tools import portfolio_tool

    positions.merge_from_broker([_row("AAPL", 20, 300)])
    positions.add("NVDA", 10, cost=180, stop=165, invalidation="capex cuts")

    out = portfolio_tool.holdings_needing_rules()

    assert "AAPL" in out
    assert "no stop" in out
    assert "never alert" in out
    assert "NVDA" not in out, "it has both"


def test_everything_configured_says_so():
    from ada.tools import portfolio_tool

    positions.add("NVDA", 10, cost=180, stop=165, invalidation="capex cuts")

    assert "Every holding has" in portfolio_tool.holdings_needing_rules()


def test_a_disabled_connection_is_reported_as_needing_attention(monkeypatch, keyed):
    """A connection that has quietly gone stale looks exactly like a portfolio
    that stopped changing."""
    from ada.tools import portfolio_tool

    monkeypatch.setattr(
        brokerage, "connections",
        lambda: [{"brokerage": {"name": "Robinhood"}, "disabled": True}],
    )

    out = portfolio_tool.connection_status()

    assert "Robinhood" in out
    assert "DISABLED" in out


# --- the heartbeat check ----------------------------------------------------


def test_the_sync_check_is_off_by_default():
    """It merges into the positions every alert runs on. Worth watching once
    by hand before it runs while you're asleep."""
    import json
    from pathlib import Path

    config = json.loads((Path(__file__).resolve().parent.parent / "ada_config.json").read_text())

    assert config["heartbeat"]["checks"]["sync_portfolio"]["enabled"] is False


def test_the_sync_check_is_registered():
    from ada.heartbeat.checks import CHECKS
    from ada.heartbeat.sync_check import check_sync

    assert CHECKS["sync_portfolio"] is check_sync


def test_nothing_syncs_when_the_market_is_shut(monkeypatch, broker, keyed):
    from ada.heartbeat import sync_check

    monkeypatch.setattr(sync_check, "market_open", lambda now=None: False)
    sync_check.check_sync({})

    assert positions.load_positions() == []


def test_an_unconfigured_connection_is_a_setting_not_an_incident(monkeypatch, sent_notifications):
    """A notice every fifteen minutes about a feature you never turned on
    would be its own problem."""
    from ada.heartbeat import notices, sync_check

    monkeypatch.setattr(sync_check, "market_open", lambda now=None: True)
    monkeypatch.delenv(brokerage.CLIENT_ID_VAR, raising=False)
    monkeypatch.delenv(brokerage.CONSUMER_KEY_VAR, raising=False)

    sync_check.check_sync({})

    assert notices.list_notices() == []


def test_a_flaky_brokerage_does_not_look_like_something_happening_to_your_money(
    monkeypatch, sent_notifications, capsys, keyed
):
    from ada.heartbeat import notices, sync_check

    monkeypatch.setattr(sync_check, "market_open", lambda now=None: True)
    monkeypatch.setattr(brokerage, "accounts", lambda: (_ for _ in ()).throw(brokerage.BrokerageError("503")))

    sync_check.check_sync({})

    assert notices.list_notices() == []
    assert "couldn't sync" in capsys.readouterr().out


def test_a_new_holding_is_raised_because_it_will_never_alert(monkeypatch, broker, keyed, sent_notifications):
    from ada.heartbeat import notices, sync_check

    monkeypatch.setattr(sync_check, "market_open", lambda now=None: True)
    sync_check.check_sync({})

    raised = notices.list_notices()
    assert len(raised) == 1
    assert "New in your brokerage" in raised[0]["text"]
    assert "won't alert" in raised[0]["text"]


def test_a_share_count_moving_on_its_own_is_not_news(monkeypatch, broker, keyed, sent_notifications):
    from ada.heartbeat import notices, sync_check

    monkeypatch.setattr(sync_check, "market_open", lambda now=None: True)
    sync_check.check_sync({})
    for notice in notices.list_notices():
        notices.dismiss_notice(notice["id"])

    broker["holdings"]["positions"][0]["units"] = 15
    sync_check.check_sync({})

    assert notices.list_notices() == [], "an updated position is not worth interrupting for"


def test_a_vanished_holding_is_raised_as_a_possible_sync_fault(
    monkeypatch, broker, keyed, sent_notifications
):
    from ada.heartbeat import notices, sync_check

    monkeypatch.setattr(sync_check, "market_open", lambda now=None: True)
    sync_check.check_sync({})
    for notice in notices.list_notices():
        notices.dismiss_notice(notice["id"])

    broker["holdings"]["positions"] = []
    sync_check.check_sync({})

    text = notices.list_notices()[0]["text"]
    assert "no longer reports" in text
    assert "sync fault" in text


# --- the dashboard ----------------------------------------------------------


def test_the_accounts_endpoint_serves_the_last_sync_and_never_calls_out(broker, keyed, monkeypatch):
    from ada.face.app import app

    portfolio.sync()
    monkeypatch.setattr(brokerage, "accounts", lambda: (_ for _ in ()).throw(AssertionError("called out")))

    body = app.test_client().get("/api/accounts").get_json()

    assert body["accounts"][0]["number"] == "••••9012"
    assert body["cash"] == 2500.0
    assert body["at"] is not None


def test_the_accounts_endpoint_before_any_sync_is_empty_not_broken():
    from ada.face.app import app

    body = app.test_client().get("/api/accounts").get_json()

    assert body["accounts"] == []
    assert body["at"] is None
