"""Reminders that fire: the timestamp round-trip, and the check that alerts."""

import time
from datetime import datetime, timedelta

import pytest

from ada.heartbeat import checks, notices
from ada.tools import reminders


def _iso(**delta):
    return (datetime.now() + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S")


def _due_check(**params):
    checks.check_due_reminders({"level": "urgent", **params})


# --- storing a real time ----------------------------------------------------


def test_due_at_is_stored_as_a_real_timestamp():
    reminders.add_reminder("call the dentist", due="tomorrow at 3", due_at=_iso(days=1))

    stored = reminders.load_reminders()[0]

    assert isinstance(stored["due_at"], float)
    assert stored["due"] == "tomorrow at 3", "the human phrasing is kept for display"
    assert stored["due_at"] > time.time()


def test_a_reminder_with_no_time_is_stored_unscheduled():
    reminders.add_reminder("think about the garden")

    assert reminders.load_reminders()[0]["due_at"] is None


def test_an_unparseable_time_is_a_clear_error_not_a_silent_miss():
    """The model must see and correct this, not create a reminder that never
    fires while the reply claims it will."""
    with pytest.raises(ValueError, match="ISO 8601"):
        reminders.add_reminder("x", due_at="next tuesday-ish")

    assert reminders.load_reminders() == [], "nothing is stored when the time is rejected"


def test_listing_says_when_a_reminder_cannot_alert():
    reminders.add_reminder("vague thing", due="sometime soon")

    assert "no exact time set" in reminders.list_reminders()


def test_listing_puts_the_soonest_first_and_undated_last():
    reminders.add_reminder("later", due_at=_iso(days=3))
    reminders.add_reminder("undated")
    reminders.add_reminder("sooner", due_at=_iso(hours=1))

    listed = reminders.list_reminders()

    assert listed.index("sooner") < listed.index("later") < listed.index("undated")


def test_an_overdue_reminder_is_marked_overdue():
    reminders.add_reminder("missed it", due_at=_iso(hours=-2))

    assert "overdue" in reminders.list_reminders()


# --- the check that fires ---------------------------------------------------


def test_a_reminder_fires_when_its_time_arrives():
    reminders.add_reminder("call the dentist", due_at=_iso(seconds=-1))

    _due_check()

    assert len(notices.list_notices()) == 1
    assert "call the dentist" in notices.list_notices()[0]["text"]
    assert notices.list_notices()[0]["level"] == "urgent"


def test_a_reminder_does_not_fire_early():
    reminders.add_reminder("not yet", due_at=_iso(hours=2))

    _due_check()

    assert notices.list_notices() == []


def test_lead_minutes_fires_a_reminder_ahead_of_time():
    reminders.add_reminder("leave for the train", due_at=_iso(minutes=10))

    _due_check(lead_minutes=15)

    assert len(notices.list_notices()) == 1


def test_a_reminder_fires_once_and_not_on_every_tick():
    """The check runs every 60s by default — re-firing would be relentless."""
    reminders.add_reminder("call the dentist", due_at=_iso(seconds=-1))

    for _ in range(5):
        _due_check()

    assert len(notices.list_notices()) == 1


def test_a_completed_reminder_never_fires():
    reminders.add_reminder("already done", due_at=_iso(seconds=-1))
    reminders.complete_reminder(reminders.load_reminders()[0]["id"])

    _due_check()

    assert notices.list_notices() == []


def test_an_undated_reminder_is_never_fired_by_the_due_check():
    reminders.add_reminder("no time on this one")

    _due_check()

    assert notices.list_notices() == []


def test_a_reminder_overdue_while_the_app_was_closed_still_fires():
    """Closing the laptop should delay an alert, never lose it — this is the
    catch-up pass the heartbeat runs at startup."""
    reminders.add_reminder("due while you were away", due_at=_iso(hours=-9))

    _due_check()

    assert len(notices.list_notices()) == 1


def test_the_notice_level_is_configurable_not_hardcoded():
    reminders.add_reminder("wake me", due_at=_iso(seconds=-1))

    _due_check(level="critical")

    assert notices.list_notices()[0]["level"] == "critical"


# --- snoozing ---------------------------------------------------------------


def test_snoozing_pushes_the_alert_back_and_re_arms_it():
    reminders.add_reminder("call the dentist", due_at=_iso(seconds=-1))
    _due_check()
    reminder_id = reminders.load_reminders()[0]["id"]

    reminders.snooze_reminder(reminder_id, _iso(seconds=-1))
    _due_check()

    assert len(notices.list_notices()) == 2, "a snoozed reminder must be able to fire again"


def test_snoozing_into_the_future_stays_quiet():
    reminders.add_reminder("call the dentist", due_at=_iso(seconds=-1))
    reminder_id = reminders.load_reminders()[0]["id"]

    reminders.snooze_reminder(reminder_id, _iso(hours=3))
    _due_check()

    assert notices.list_notices() == []


def test_snoozing_a_missing_reminder_raises():
    with pytest.raises(ValueError, match="No reminder with id"):
        reminders.snooze_reminder(999, _iso(hours=1))


# --- staleness no longer overlaps with due ----------------------------------


def test_a_scheduled_reminder_is_not_also_nagged_as_stale():
    """One due next month isn't stale, it just isn't due yet."""
    reminders.add_reminder("dentist next month", due_at=_iso(days=30))
    stored = reminders.load_reminders()
    stored[0]["created_at"] = time.time() - 48 * 3600
    reminders._save(stored)

    checks.check_stale_reminders({"stale_after_hours": 24})

    assert notices.list_notices() == []


def test_an_undated_reminder_still_goes_stale():
    reminders.add_reminder("undated and forgotten")
    stored = reminders.load_reminders()
    stored[0]["created_at"] = time.time() - 48 * 3600
    reminders._save(stored)

    checks.check_stale_reminders({"stale_after_hours": 24})

    assert len(notices.list_notices()) == 1
