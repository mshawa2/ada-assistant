"""The nudge about unguarded positions.

Every test here is really "does it shut up". The failure mode this check exists
to avoid is not missing a position — it is being so talkative about 47 of them
that the user stops reading notices altogether, which would break every other
alert Ada has.

`market.quote` is faked; nothing reaches the network.
"""

import time

import pytest

from ada import market, positions, store
from ada.heartbeat import guard_check, notices


@pytest.fixture(autouse=True)
def no_quotes(monkeypatch):
    monkeypatch.setattr(market, "quote", lambda symbol, span="5d": {"price": 100.0})
    yield


@pytest.fixture
def book():
    positions.add("BIG", shares=100, cost=50.0)
    positions.add("SMALL", shares=1, cost=10.0)


def state() -> dict:
    return store.read_json(guard_check._STATE, {})


def test_it_asks_about_one_position_not_all_of_them(book):
    guard_check.check_unguarded({})
    assert len(notices.list_notices()) == 1


def test_it_asks_about_the_largest_holding_first(book):
    guard_check.check_unguarded({})
    assert "BIG" in notices.list_notices()[0]["text"]


def test_it_stays_quiet_until_the_interval_passes(book):
    guard_check.check_unguarded({})
    for _ in range(5):
        guard_check.check_unguarded({})

    assert len(notices.list_notices()) == 1


def test_it_moves_on_to_the_next_position(book):
    guard_check.check_unguarded({"every_seconds": 0})
    guard_check.check_unguarded({"every_seconds": 0})

    said = " ".join(n["text"] for n in notices.list_notices())
    assert "BIG" in said and "SMALL" in said


def test_it_does_not_ask_about_the_same_symbol_twice_in_a_month(book):
    guard_check.check_unguarded({"every_seconds": 0})
    guard_check.check_unguarded({"every_seconds": 0})
    # Both asked; a third pass has nobody left outside the cooldown.
    guard_check.check_unguarded({"every_seconds": 0})

    assert len(notices.list_notices()) == 2


def test_setting_a_stop_removes_a_position_from_the_list(book):
    positions.update("BIG", stop=45.0)
    guard_check.check_unguarded({})

    assert "SMALL" in notices.list_notices()[0]["text"]


def test_a_thesis_also_removes_it(book):
    positions.update("BIG", thesis="holding through the cycle")
    guard_check.check_unguarded({})

    assert "SMALL" in notices.list_notices()[0]["text"]


def test_nothing_is_said_when_every_position_is_guarded(book):
    positions.update("BIG", stop=45.0)
    positions.update("SMALL", thesis="speculative")
    guard_check.check_unguarded({})

    assert notices.list_notices() == []


def test_it_offers_arithmetic_rather_than_a_number_to_use(book):
    """A stop is the user's judgement; Ada does the sum and stops there."""
    guard_check.check_unguarded({})
    text = notices.list_notices()[0]["text"]

    assert "10% = $90.00" in text
    assert "15% = $85.00" in text
    assert "you should" not in text.lower()


def test_it_says_how_many_others_are_in_the_same_state(book):
    guard_check.check_unguarded({})
    assert "2 holdings are in the same state" in notices.list_notices()[0]["text"]


def test_a_quote_failure_still_produces_a_useful_nudge(book, monkeypatch):
    def boom(symbol, span="5d"):
        raise RuntimeError("yahoo said no")

    monkeypatch.setattr(market, "quote", boom)
    guard_check.check_unguarded({})

    text = notices.list_notices()[0]["text"]
    assert "no stop and no thesis" in text
    assert "cost $" in text  # cost basis is local, so it survives


def test_an_empty_book_says_nothing():
    guard_check.check_unguarded({})
    assert notices.list_notices() == []
    assert state().get("last")  # but the pass is recorded


def test_the_ask_is_remembered(book):
    guard_check.check_unguarded({})
    assert "BIG" in (state().get("asked") or {})


def test_a_recorded_ask_survives_a_later_quiet_pass(book):
    guard_check.check_unguarded({"every_seconds": 0})
    positions.update("SMALL", stop=5.0)
    guard_check.check_unguarded({"every_seconds": 0, "cooldown_seconds": 999999})

    # BIG was asked, SMALL is now guarded, so nothing new is said — and the
    # earlier ask is still on record rather than being reset.
    assert len(notices.list_notices()) == 1
    assert "BIG" in (state().get("asked") or {})


def test_time_moves_the_cooldown_along(book):
    guard_check.check_unguarded({})
    data = state()
    data["last"] = 0
    data["asked"] = {"BIG": time.time() - 40 * 86400}
    store.write_json(guard_check._STATE, data)

    guard_check.check_unguarded({})
    assert "BIG" in notices.list_notices()[-1]["text"]
