"""The morning push: when it fires, when it deliberately doesn't, and what it
does with yesterday's."""

import json
import pathlib
import time

import pytest

from ada import brief, schedules
from ada.heartbeat import brief_check, notices
from ada.heartbeat.state import get_check_state, update_check_state

A_DAY = 86400


@pytest.fixture(autouse=True)
def a_brief_worth_pushing(monkeypatch):
    monkeypatch.setattr(brief, "compose", lambda: "Overdue (1):\n  - #1 call the dentist")


def _armed_for(seconds_from_now: float) -> None:
    update_check_state(brief.PUSH_CHECK, push_due_at=time.time() + seconds_from_now)


def _pushed() -> list[dict]:
    return [n for n in notices.list_notices() if n["check"] == brief.PUSH_CHECK]


# --- arming -----------------------------------------------------------------


def test_the_first_run_arms_rather_than_firing():
    """Turning this on at three in the afternoon must not produce a 'morning'
    brief at three in the afternoon."""
    brief_check.check_morning_brief({"at": "08:00"})

    assert _pushed() == []
    assert get_check_state(brief.PUSH_CHECK)["push_due_at"] > time.time()


def test_it_arms_for_the_configured_time_in_the_users_timezone():
    brief_check.check_morning_brief({"at": "08:00"})

    armed = get_check_state(brief.PUSH_CHECK)["push_due_at"]
    assert armed == schedules.next_run({"repeat": "daily", "at": "08:00"})


def test_nothing_fires_before_its_time():
    _armed_for(A_DAY)

    brief_check.check_morning_brief({"at": "08:00"})

    assert _pushed() == []


# --- firing -----------------------------------------------------------------


def test_it_fires_once_its_time_has_come(sent_notifications):
    _armed_for(-60)

    brief_check.check_morning_brief({"at": "08:00"})

    assert len(_pushed()) == 1
    assert "call the dentist" in _pushed()[0]["text"]
    assert sent_notifications, "the whole point is that it reaches you away from the Mac"


def test_it_fires_once_not_on_every_tick(sent_notifications):
    _armed_for(-60)

    for _ in range(10):
        brief_check.check_morning_brief({"at": "08:00"})

    assert len(_pushed()) == 1


def test_a_missed_morning_collapses_to_one_brief(sent_notifications):
    """Opening a laptop after a week away to seven identical briefings is
    worse than none — the same promise schedules.py makes."""
    _armed_for(-3 * A_DAY)

    brief_check.check_morning_brief({"at": "08:00", "catch_up_hours": 1000})

    assert len(_pushed()) == 1
    assert get_check_state(brief.PUSH_CHECK)["push_due_at"] > time.time()


# --- refusing to fire late --------------------------------------------------


def test_a_brief_too_late_to_be_a_morning_one_is_skipped(sent_notifications):
    """A push saying 'here's your morning' at ten at night is how someone
    learns to turn notifications off."""
    _armed_for(-10 * 3600)

    brief_check.check_morning_brief({"at": "08:00", "catch_up_hours": 4})

    assert _pushed() == []
    assert sent_notifications == []


def test_a_skipped_brief_still_re_arms_for_tomorrow():
    """Otherwise it would be past its catch-up window forever and never fire
    again — a silent, permanent failure."""
    _armed_for(-10 * 3600)

    brief_check.check_morning_brief({"at": "08:00", "catch_up_hours": 4})

    assert get_check_state(brief.PUSH_CHECK)["push_due_at"] > time.time()


def test_a_brief_within_the_catch_up_window_still_goes_out(sent_notifications):
    """Opening the laptop at half nine should still get you your morning."""
    _armed_for(-90 * 60)

    brief_check.check_morning_brief({"at": "08:00", "catch_up_hours": 4})

    assert len(_pushed()) == 1


# --- saying nothing loudly --------------------------------------------------


def test_an_empty_day_is_not_pushed(monkeypatch, sent_notifications):
    """A daily notification whose usual content is 'nothing' trains you to
    swipe it away unread, which costs you the morning it isn't."""
    monkeypatch.setattr(brief, "compose", lambda: brief.NOTHING)
    _armed_for(-60)

    brief_check.check_morning_brief({"at": "08:00"})

    assert _pushed() == []
    assert sent_notifications == []


def test_an_empty_day_can_be_pushed_if_you_want_the_reassurance(monkeypatch, sent_notifications):
    monkeypatch.setattr(brief, "compose", lambda: brief.NOTHING)
    _armed_for(-60)

    brief_check.check_morning_brief({"at": "08:00", "push_when_empty": True})

    assert len(_pushed()) == 1


# --- not stacking up --------------------------------------------------------


def test_todays_brief_retires_yesterdays(sent_notifications):
    _armed_for(-60)
    brief_check.check_morning_brief({"at": "08:00"})
    _armed_for(-60)

    brief_check.check_morning_brief({"at": "08:00"})

    assert len(_pushed()) == 1, "a week of undismissed briefs is not a greeting"


# --- wiring -----------------------------------------------------------------


def test_the_check_is_registered():
    from ada.heartbeat.checks import CHECKS

    assert CHECKS["morning_brief"] is brief_check.check_morning_brief


def test_the_shipped_config_turns_it_on_and_looks_at_the_clock_often_enough():
    """`interval_seconds` is only how often it checks the clock — a fifteen
    minute interval would land an 08:00 brief at 08:15."""
    config = json.loads(pathlib.Path("ada_config.json").read_text())
    entry = config["heartbeat"]["checks"]["morning_brief"]

    assert entry["enabled"] is True
    assert entry["interval_seconds"] <= 900
