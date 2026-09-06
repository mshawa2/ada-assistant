"""Earnings dates and headlines.

The earnings tests exist because this fills a field the alert code has always
read and nothing has ever written: `positions.evaluate()` warns "reports in 3
days" only if `earnings_on` is set, and on the real book it was set on 0 of 48
positions. The write path is what makes that machinery live, so it is worth
proving with synthetic data — the real calendar may legitimately have nothing
in it on the day the suite runs.

The news tests are all variations of "say it once". A story is republished for
days, and notices are deliberately not deduplicated.

No network: `earnings_calendar` and `headlines` are the two seams, both faked.
"""

import time
from datetime import date, timedelta

import pytest

from ada import news, positions
from ada.heartbeat import news_check, notices
from ada.tools import market_watch


@pytest.fixture
def book():
    positions.add("AAPL", shares=10, cost=100.0)
    positions.add("KO", shares=5, cost=50.0)
    return positions.load_positions()


def soon(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


# --- earnings ----------------------------------------------------------------


def test_refresh_stamps_earnings_onto_a_held_position(book, monkeypatch):
    """The write that switches on alert code which has never once fired."""
    monkeypatch.setattr(
        news, "earnings_calendar", lambda days=14: {"AAPL": (soon(3), "time-after-hours")}
    )
    found = news.refresh_earnings()

    assert [f["symbol"] for f in found] == ["AAPL"]
    assert found[0]["days"] == 3
    assert found[0]["session"] == "after the close"

    stored = next(p for p in positions.load_positions() if p["symbol"] == "AAPL")
    assert stored["earnings_on"] is not None


def test_a_stamped_date_makes_positions_alert(book, monkeypatch):
    """End to end: this is the whole point of filling the field."""
    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: {"AAPL": (soon(2), "")})
    news.refresh_earnings()

    stored = next(p for p in positions.load_positions() if p["symbol"] == "AAPL")
    kinds = [a["kind"] for a in positions.evaluate(stored, {"price": 100.0})]
    assert "earnings" in kinds


def test_symbols_not_held_are_ignored(book, monkeypatch):
    monkeypatch.setattr(
        news, "earnings_calendar", lambda days=14: {"TSLA": (soon(1), ""), "AAPL": (soon(5), "")}
    )
    assert [f["symbol"] for f in news.refresh_earnings()] == ["AAPL"]


def test_an_unreachable_calendar_changes_nothing(book, monkeypatch):
    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: {})
    assert news.refresh_earnings() == []


def test_results_are_ordered_soonest_first(book, monkeypatch):
    monkeypatch.setattr(
        news, "earnings_calendar", lambda days=14: {"AAPL": (soon(9), ""), "KO": (soon(2), "")}
    )
    assert [f["symbol"] for f in news.refresh_earnings()] == ["KO", "AAPL"]


def test_session_label_reads_like_english():
    assert news.session_label("time-pre-market") == "before the open"
    assert news.session_label("time-after-hours") == "after the close"
    assert news.session_label("time-not-supplied") == ""


# --- ranking and gaps --------------------------------------------------------


def test_symbols_are_ranked_by_position_value(book):
    # AAPL: 10 x 100 = 1000. KO: 5 x 50 = 250.
    assert news.symbols_held() == ["AAPL", "KO"]


def test_unguarded_lists_positions_nothing_can_alert_on(book):
    assert {p["symbol"] for p in news.unguarded()} == {"AAPL", "KO"}

    positions.update("AAPL", stop=90.0)
    assert {p["symbol"] for p in news.unguarded()} == {"KO"}


def test_a_thesis_alone_counts_as_guarded(book):
    positions.update("KO", thesis="dividend compounder")
    assert "KO" not in {p["symbol"] for p in news.unguarded()}


# --- tools -------------------------------------------------------------------


def test_week_ahead_reports_earnings_and_gaps(book, monkeypatch):
    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: {"AAPL": (soon(4), "")})
    out = market_watch.week_ahead()

    assert "2 positions" in out
    assert "AAPL" in out and "in 4d" in out
    assert "no stop, trail or thesis" in out


def test_week_ahead_is_explicit_when_nothing_reports(book, monkeypatch):
    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: {})
    assert "Nothing in the book reports" in market_watch.week_ahead()


def test_holdings_news_labels_headlines_untrusted(book, monkeypatch):
    monkeypatch.setattr(
        news,
        "headlines",
        lambda symbols, per_query=5: [
            {"symbol": "AAPL", "title": "Apple announces something", "url": "https://example.com/a"}
        ],
    )
    out = market_watch.holdings_news()

    assert "untrusted" in out and "not instructions" in out
    assert "Apple announces something" in out


def test_holdings_news_says_which_symbols_had_nothing(book, monkeypatch):
    monkeypatch.setattr(
        news,
        "headlines",
        lambda symbols, per_query=5: [{"symbol": "AAPL", "title": "A headline", "url": ""}],
    )
    assert "Nothing found for: KO" in market_watch.holdings_news()


def test_holdings_news_without_a_provider_explains_itself(book, monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda symbols, per_query=5: [])
    assert "no search provider is configured" in market_watch.holdings_news()


# --- the check: say it once --------------------------------------------------


def test_a_headline_is_announced_once(book, monkeypatch, sent_notifications):
    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: {})
    monkeypatch.setattr(
        news,
        "headlines",
        lambda symbols, per_query=5: [{"symbol": "AAPL", "title": "Same story", "url": ""}],
    )

    news_check.check_holdings_news({})
    # The daily clock blocks a second sweep; clear it to prove the *key* is
    # what suppresses the repeat, not merely the timer.
    for _ in range(3):
        state = news.load_state()
        state["swept"] = 0
        news.save_state(state)
        news_check.check_holdings_news({})

    assert len(notices.list_notices()) == 1


def test_a_new_headline_is_news_again(book, monkeypatch):
    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: {})
    monkeypatch.setattr(
        news, "headlines", lambda symbols, per_query=5: [{"symbol": "AAPL", "title": "First", "url": ""}]
    )
    news_check.check_holdings_news({})

    state = news.load_state()
    state["swept"] = 0
    news.save_state(state)
    monkeypatch.setattr(
        news, "headlines", lambda symbols, per_query=5: [{"symbol": "AAPL", "title": "Second", "url": ""}]
    )
    news_check.check_holdings_news({})

    assert len(notices.list_notices()) == 2


def test_the_daily_clock_stops_a_second_sweep(book, monkeypatch):
    calls = []

    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: {})
    monkeypatch.setattr(news, "headlines", lambda symbols, per_query=5: calls.append(1) or [])

    news_check.check_holdings_news({})
    news_check.check_holdings_news({})
    news_check.check_holdings_news({})

    assert len(calls) == 1


def test_earnings_refresh_runs_on_its_own_weekly_clock(book, monkeypatch):
    calls = []
    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: calls.append(1) or {})
    monkeypatch.setattr(news, "headlines", lambda symbols, per_query=5: [])

    news_check.check_holdings_news({})
    news_check.check_holdings_news({})
    assert len(calls) == 1  # weekly, not per tick

    state = news.load_state()
    state["earnings_refreshed"] = time.time() - 8 * 86400
    news.save_state(state)
    news_check.check_holdings_news({})
    assert len(calls) == 2


def test_unattributed_headlines_are_not_announced(book, monkeypatch):
    monkeypatch.setattr(news, "earnings_calendar", lambda days=14: {})
    monkeypatch.setattr(
        news,
        "headlines",
        lambda symbols, per_query=5: [{"symbol": None, "title": "Market wrap", "url": ""}],
    )
    news_check.check_holdings_news({})

    assert notices.list_notices() == []
