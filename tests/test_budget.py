"""Spending limits.

Nothing here makes a real model call — the one test that touches `llm` checks
that it refuses *before* reaching the API, which is the whole point.
"""

import time

import pytest

from ada import budget, cost, llm, settings, store
from ada.tools import settings_tool


def _limits(model=None, purchases=None, currency="USD"):
    limits = {}
    if model is not None:
        limits["model"] = model
    if purchases is not None:
        limits["purchases"] = purchases
    store.write_json(settings._CONFIG_FILE, {"budget": {"currency": currency, "limits": limits}})
    settings._cache["stamp"] = None


@pytest.fixture(autouse=True)
def reset_session_tally(monkeypatch):
    """cost.py keeps a module-level tally; don't let it leak between tests."""
    monkeypatch.setattr(cost, "_input_tokens", 0)
    monkeypatch.setattr(cost, "_output_tokens", 0)
    monkeypatch.setattr(cost, "_cache_read_tokens", 0)
    monkeypatch.setattr(cost, "_cache_write_tokens", 0)


# --- the ledger -------------------------------------------------------------


def test_nothing_is_spent_to_begin_with():
    _limits(model={"per_day": 5.0})

    assert budget.spent(budget.MODEL, budget.WINDOWS["per_day"]) == 0.0


def test_spending_accumulates():
    _limits(model={"per_day": 5.0})

    budget.record(budget.MODEL, 0.25)
    budget.record(budget.MODEL, 0.75)

    assert budget.spent(budget.MODEL, budget.WINDOWS["per_day"]) == 1.0


def test_spend_outside_the_window_does_not_count():
    _limits(model={"per_day": 5.0, "per_month": 50.0})
    store.write_json(
        budget._STORE,
        {"model": [{"at": time.time() - 90000, "amount": 4.0, "note": "yesterday"}]},
    )

    assert budget.spent(budget.MODEL, budget.WINDOWS["per_day"]) == 0.0
    assert budget.spent(budget.MODEL, budget.WINDOWS["per_month"]) == 4.0


def test_a_zero_or_negative_amount_is_ignored():
    _limits(model={"per_day": 5.0})

    budget.record(budget.MODEL, 0.0)
    budget.record(budget.MODEL, -1.0)

    assert budget.spent(budget.MODEL, budget.WINDOWS["per_day"]) == 0.0


def test_uncapped_categories_are_never_blocked():
    _limits(model={"per_day": 5.0})

    budget.record("something_else", 1000.0)
    budget.check("something_else", 1000.0)  # must not raise


# --- the ceiling ------------------------------------------------------------


def test_under_the_cap_is_allowed():
    _limits(model={"per_day": 5.0})
    budget.record(budget.MODEL, 1.0)

    budget.check(budget.MODEL)  # must not raise


def test_reaching_the_cap_blocks_further_spending():
    _limits(model={"per_day": 5.0})
    budget.record(budget.MODEL, 5.0)

    with pytest.raises(budget.BudgetExceeded, match="Spending limit reached"):
        budget.check(budget.MODEL)


def test_a_known_amount_is_checked_before_it_is_spent():
    """A purchase knows its price up front, so it can be refused rather than
    discovered afterwards."""
    # Both windows, because the defaults cap purchases at zero and settings
    # merge additively — leaving per_month unset would breach that first.
    _limits(purchases={"per_day": 20.0, "per_month": 20.0})
    budget.record(budget.PURCHASES, 15.0)

    budget.check(budget.PURCHASES, 4.0)  # fine

    with pytest.raises(budget.BudgetExceeded, match="would take it to"):
        budget.check(budget.PURCHASES, 10.0)


def test_any_breached_window_blocks_even_if_others_have_room():
    _limits(model={"per_day": 100.0, "per_month": 10.0})
    budget.record(budget.MODEL, 10.0)

    with pytest.raises(budget.BudgetExceeded, match="month"):
        budget.check(budget.MODEL)


def test_purchases_default_to_a_zero_ceiling():
    """The rail exists before the capability: the day a purchasing tool is
    added, it is denied until the user deliberately raises this."""
    assert settings.DEFAULTS["budget"]["limits"]["purchases"]["per_day"] == 0.0

    _limits(purchases={"per_day": 0.0})
    with pytest.raises(budget.BudgetExceeded):
        budget.check(budget.PURCHASES, 0.01)


def test_the_error_tells_the_model_not_to_retry():
    _limits(model={"per_day": 1.0})
    budget.record(budget.MODEL, 1.0)

    with pytest.raises(budget.BudgetExceeded, match="don't retry"):
        budget.check(budget.MODEL)


# --- pricing and display ----------------------------------------------------


def test_cost_is_priced_from_settings():
    store.write_json(
        settings._CONFIG_FILE,
        {"model": {"pricing": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}},
    )
    settings._cache["stamp"] = None

    assert cost.price_of(1_000_000, 0) == 3.0
    assert cost.price_of(0, 1_000_000) == 15.0


def test_recording_usage_reaches_the_ledger():
    _limits(model={"per_day": 5.0})

    cost.record(1_000_000, 0)

    assert budget.spent(budget.MODEL, budget.WINDOWS["per_day"]) == pytest.approx(2.0)


def test_small_amounts_are_not_rounded_away():
    """Model costs are fractions of a cent; rounding to pennies would erase
    the entire tally."""
    assert budget.money(0.0123) != "$0.01"
    assert "0.0123" in budget.money(0.0123)


def test_larger_amounts_read_as_money():
    assert budget.money(12.5) == "$12.50"


def test_the_currency_symbol_follows_the_setting():
    _limits(model={"per_day": 5.0}, currency="GBP")

    assert budget.money(12.5).startswith("£")


def test_summary_reports_usage_against_each_cap():
    _limits(model={"per_day": 5.0})
    budget.record(budget.MODEL, 1.25)

    summary = budget.summary()

    assert "model" in summary
    assert "$1.25" in summary
    assert "$3.75 left" in summary


def test_summary_says_so_when_at_the_limit():
    _limits(model={"per_day": 1.0})
    budget.record(budget.MODEL, 1.0)

    assert "at the limit" in budget.summary()


def test_a_null_cap_turns_a_window_off():
    """Settings merge additively, so an empty block can't remove an inherited
    default — null is the way to disable one, and it must disable it in both
    the check and the summary."""
    store.write_json(
        settings._CONFIG_FILE,
        {
            "budget": {
                "limits": {
                    "model": {"per_day": None, "per_month": None},
                    "purchases": {"per_day": None, "per_month": None},
                }
            }
        },
    )
    settings._cache["stamp"] = None
    budget.record(budget.MODEL, 999.0)

    budget.check(budget.MODEL)  # must not raise
    assert "No spending limits" in budget.summary()


def test_the_session_summary_still_reports_tokens_exactly():
    _limits(model={"per_day": 5.0})
    cost.record(1200, 340)

    summary = cost.summary()

    assert "1,200 input" in summary
    assert "340 output" in summary
    assert "approximate" in summary, "the dollar figure is an estimate and says so"


# --- enforcement at the model call ------------------------------------------


def test_the_model_refuses_to_spend_past_the_cap():
    """Checked before the request, not after — discovering you're over budget
    once the money is gone isn't a limit, it's a receipt."""
    _limits(model={"per_day": 1.0})
    budget.record(budget.MODEL, 1.0)

    generator = llm.stream_turn([{"role": "user", "content": "hi"}], tools=[])

    with pytest.raises(llm.LLMError, match="Spending limit reached"):
        next(generator)


def test_the_model_runs_normally_under_the_cap(monkeypatch):
    _limits(model={"per_day": 5.0})

    # Fail at the API boundary, proving the budget check let it through.
    def boom(*_args, **_kwargs):
        raise AssertionError("reached the API")

    monkeypatch.setattr(llm._client.messages, "stream", boom)

    with pytest.raises(AssertionError, match="reached the API"):
        next(llm.stream_turn([{"role": "user", "content": "hi"}], tools=[]))


# --- the tool ---------------------------------------------------------------


def test_get_spending_reports_the_summary():
    _limits(model={"per_day": 5.0})
    budget.record(budget.MODEL, 2.0)

    result = settings_tool.get_spending()

    assert "model" in result
    assert "$2.00" in result


def test_get_spending_is_registered():
    from ada.tools import specs

    assert "get_spending" in {spec["name"] for spec in specs()}
