"""Prompt caching.

Caching is a prefix match, which makes it unusually easy to break by accident:
put anything that changes on its own above the breakpoint and the cache stops
being read — silently, with no error and nothing different about the reply. Only
the bill changes.

So the tests that matter here aren't about the cache API. They pin the *shape*
of what gets sent — the volatile clock stays below the breakpoint — and that
cached tokens are priced as cached rather than counted as fresh input.
"""

import re
from datetime import datetime, timezone

import pytest

from ada import budget, cost, llm, settings, store, system_prompt

_CLOCK = re.compile(r"\d{1,2}:\d{2}")

# Two moments a day apart. The cached half of the prompt must read identically
# at both; the uncached half must not.
_AT_NINE = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
_NEXT_MORNING = datetime(2026, 8, 18, 10, 31, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def reset_session_tally(monkeypatch):
    monkeypatch.setattr(cost, "_input_tokens", 0)
    monkeypatch.setattr(cost, "_output_tokens", 0)
    monkeypatch.setattr(cost, "_cache_read_tokens", 0)
    monkeypatch.setattr(cost, "_cache_write_tokens", 0)


# --- what may sit above the breakpoint --------------------------------------


def test_the_stable_prompt_does_not_move_when_the_clock_does(monkeypatch):
    """The regression this split exists to prevent: a live timestamp above the
    breakpoint re-bills the prompt and every tool schema at full price on every
    single call.

    Asserted by moving the clock rather than by searching for something that
    looks like a time — the reminder instructions quote an example timestamp,
    which is a literal and costs nothing. What matters is only whether the
    bytes change on their own.
    """
    monkeypatch.setattr(settings, "now", lambda: _AT_NINE)
    at_nine = system_prompt.stable_prompt()
    monkeypatch.setattr(settings, "now", lambda: _NEXT_MORNING)

    assert system_prompt.stable_prompt() == at_nine


def test_the_clock_is_still_sent_just_separately():
    """Ada can't turn "tomorrow at 3" into a real timestamp without it, so
    moving it must not mean losing it."""
    assert _CLOCK.search(system_prompt.now_line())


def test_the_stable_prompt_is_stable():
    assert system_prompt.stable_prompt() == system_prompt.stable_prompt()


def test_the_breakpoint_sits_on_the_stable_block_only(monkeypatch):
    monkeypatch.setattr(settings, "now", lambda: _AT_NINE)
    blocks = llm._system()

    assert [b["type"] for b in blocks] == ["text", "text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1], "a breakpoint below the clock would cache nothing"
    assert "09:00" in blocks[1]["text"], "the clock belongs below the breakpoint"

    monkeypatch.setattr(settings, "now", lambda: _NEXT_MORNING)
    moved = llm._system()

    assert moved[0]["text"] == blocks[0]["text"], "an hour later must still be a cache hit"
    assert moved[1]["text"] != blocks[1]["text"], "the clock must actually be live"


def test_settings_the_user_changes_stay_above_the_breakpoint():
    """Mode belongs in the cached half: it only moves when the user moves it,
    so it costs one cold write rather than one per call."""
    store.write_json(settings._CONFIG_FILE, {"modes": {"active": "focus"}})
    settings._cache["stamp"] = None

    assert "'focus' mode" in system_prompt.stable_prompt()


# --- pricing what the cache actually did -------------------------------------


def test_a_cached_read_costs_a_tenth_of_fresh_input():
    fresh = cost.price_of(1_000_000, 0)
    cached = cost.price_of(0, 0, cache_read_tokens=1_000_000)

    assert cached == pytest.approx(fresh * 0.10)


def test_writing_the_cache_costs_a_premium_over_sending_it_plainly():
    fresh = cost.price_of(1_000_000, 0)
    written = cost.price_of(0, 0, cache_write_tokens=1_000_000)

    assert written == pytest.approx(fresh * 1.25)


def test_cache_tokens_add_to_input_rather_than_overlapping_it():
    """The API reports `input_tokens` as only what was billed at full rate, so
    counting the cached ones alongside is addition, not double-counting."""
    both = cost.price_of(1_000, 0, cache_read_tokens=1_000)

    assert both == pytest.approx(
        cost.price_of(1_000, 0) + cost.price_of(0, 0, cache_read_tokens=1_000)
    )


def test_an_uncached_call_is_priced_exactly_as_before():
    """Every existing caller passes two arguments and must be unaffected."""
    assert cost.price_of(1_000_000, 0) == cost.DEFAULT_INPUT_PRICE_PER_MTOK
    assert cost.price_of(0, 1_000_000) == cost.DEFAULT_OUTPUT_PRICE_PER_MTOK


def test_a_junk_multiplier_does_not_make_a_call_free(monkeypatch):
    """Pricing is config, and config can be wrong. Falling back to the real
    multiplier keeps a typo from quietly zeroing the spend that guards the cap."""
    store.write_json(
        settings._CONFIG_FILE,
        {"model": {"pricing": {"input_per_mtok": 2.0, "output_per_mtok": 10.0}}},
    )
    settings._cache["stamp"] = None

    assert cost.price_of(0, 0, cache_read_tokens=1_000_000) > 0


# --- what the user sees ------------------------------------------------------


def test_the_session_tally_reports_what_the_cache_did():
    cost.record(100, 20, cache_read_tokens=6_775)

    assert cost.totals()["cached"] == 6_775
    assert "6,775 tokens read from cache" in cost.summary()


def test_a_cached_call_is_charged_the_cached_price_against_the_cap():
    """The budget gate reads the ledger, so pricing a 7,000-token cached call as
    7,000 fresh tokens would trip the daily cap roughly ten times too early."""
    store.write_json(
        settings._CONFIG_FILE, {"budget": {"limits": {"model": {"per_day": 5.0}}}}
    )
    settings._cache["stamp"] = None

    cost.record(111, 9, cache_read_tokens=6_775)

    charged = budget.spent(budget.MODEL, budget.WINDOWS["per_day"])
    assert charged == pytest.approx(cost.price_of(111, 9, cache_read_tokens=6_775))
    assert charged < cost.price_of(111 + 6_775, 9), "cached tokens must not bill as fresh"
