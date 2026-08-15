"""The proactive loop: that checks fire once and only once, that the kill
switch really stops them, that one broken check can't take the loop down,
and that quiet hours hold the right things back."""

import threading
import time
from datetime import datetime

from ada.heartbeat import checks, greeting, kill_switch, loop, notices, state
from ada.heartbeat.quiet_hours import in_quiet_hours
from ada.tools import reminders


def _add_aged_reminder(text="old thing", hours=48):
    reminders.add_reminder(text)
    stored = reminders.load_reminders()
    stored[-1]["created_at"] = time.time() - hours * 3600
    reminders._save(stored)
    return stored[-1]["id"]


# --- quiet hours ------------------------------------------------------------


def test_quiet_hours_window_that_wraps_past_midnight():
    window = {"start": "22:00", "end": "08:00"}

    assert in_quiet_hours(window, datetime(2026, 1, 1, 23, 0))
    assert in_quiet_hours(window, datetime(2026, 1, 1, 3, 0))
    assert not in_quiet_hours(window, datetime(2026, 1, 1, 12, 0))


def test_quiet_hours_window_inside_one_day():
    window = {"start": "09:00", "end": "17:00"}

    assert in_quiet_hours(window, datetime(2026, 1, 1, 12, 0))
    assert not in_quiet_hours(window, datetime(2026, 1, 1, 22, 0))


def test_quiet_hours_boundaries_are_start_inclusive_end_exclusive():
    window = {"start": "22:00", "end": "08:00"}

    assert in_quiet_hours(window, datetime(2026, 1, 1, 22, 0))
    assert not in_quiet_hours(window, datetime(2026, 1, 1, 8, 0))


# --- checks -----------------------------------------------------------------


def test_stale_reminder_notifies_once_and_not_again():
    _add_aged_reminder()

    checks.check_stale_reminders({"stale_after_hours": 24})
    checks.check_stale_reminders({"stale_after_hours": 24})

    assert len(notices.list_notices()) == 1, "a second pass must not re-notify"
    assert notices.list_notices()[0]["level"] == "urgent"


def test_fresh_reminder_produces_nothing():
    reminders.add_reminder("just added")

    checks.check_stale_reminders({"stale_after_hours": 24})

    assert notices.list_notices() == []


def test_completed_reminders_never_go_stale():
    reminder_id = _add_aged_reminder()
    reminders.complete_reminder(reminder_id)

    checks.check_stale_reminders({"stale_after_hours": 24})

    assert notices.list_notices() == []


def test_new_notes_seeds_silently_then_notices_a_real_addition():
    checks._NOTES_DIR.mkdir(parents=True, exist_ok=True)
    (checks._NOTES_DIR / "existing.md").write_text("already here")

    checks.check_new_notes({})
    assert notices.list_notices() == [], "the first pass ever is a baseline, not news"

    (checks._NOTES_DIR / "brand-new.md").write_text("dropped in later")
    checks.check_new_notes({})

    assert len(notices.list_notices()) == 1
    assert "brand-new.md" in notices.list_notices()[0]["text"]
    assert notices.list_notices()[0]["level"] == "normal"


def test_dismissed_notices_stay_dismissed():
    _add_aged_reminder()
    checks.check_stale_reminders({"stale_after_hours": 24})
    notice_id = notices.list_notices()[0]["id"]

    assert notices.dismiss_notice(notice_id) is True

    assert notices.list_notices() == []
    assert len(notices.list_notices(include_dismissed=True)) == 1


# --- the loop ---------------------------------------------------------------


def _config(**check_params):
    return {"tick_seconds": 0.01, "checks": {"stale_reminders": {"enabled": True, **check_params}}}


def test_paused_heartbeat_runs_nothing():
    _add_aged_reminder()
    kill_switch.set_paused(True)

    loop._run_due_checks(_config(stale_after_hours=24, interval_seconds=1))

    assert notices.list_notices() == [], "pausing must stop work, not just report paused"

    kill_switch.set_paused(False)
    loop._run_due_checks(_config(stale_after_hours=24, interval_seconds=1))
    assert len(notices.list_notices()) == 1, "anything due while paused still runs on resume"


def test_disabled_check_does_not_run():
    _add_aged_reminder()

    loop._run_due_checks({"checks": {"stale_reminders": {"enabled": False, "stale_after_hours": 24}}})

    assert notices.list_notices() == []


def test_a_check_is_not_rerun_until_its_interval_elapses():
    _add_aged_reminder()
    config = _config(stale_after_hours=24, interval_seconds=3600)

    loop._run_due_checks(config)
    _add_aged_reminder("another old thing")
    loop._run_due_checks(config)

    assert len(notices.list_notices()) == 1, "the schedule must survive a second pass"


def test_a_failing_check_neither_crashes_the_loop_nor_stalls_its_schedule(capsys):
    def explode(_params):
        raise RuntimeError("check went wrong")

    loop._run_check("boom", explode, {"interval_seconds": 60})

    assert "check failed" in capsys.readouterr().out
    assert state.get_check_state("boom")["next_due_at"] > time.time()


def test_start_catches_up_before_handing_off_to_the_thread(monkeypatch):
    """The startup greeting is built right after start() returns, so an
    overdue notice must already be logged by then — this is the race that
    was found and fixed during Tier 5 verification."""
    _add_aged_reminder()
    monkeypatch.setattr(loop, "load_config", lambda: _config(stale_after_hours=24, interval_seconds=3600))

    stop_event = threading.Event()
    thread = loop.start(stop_event)
    try:
        assert len(notices.list_notices()) == 1
    finally:
        stop_event.set()
        thread.join(timeout=2)


# --- the greeting -----------------------------------------------------------


def test_greeting_is_silent_when_there_is_nothing_to_say():
    assert greeting.startup_summary() is None


def test_quiet_hours_hold_urgent_but_never_critical(monkeypatch):
    monkeypatch.setattr(greeting, "in_quiet_hours", lambda _window: True)
    notices.add_notice("t", "urgent thing", level="urgent")
    notices.add_notice("t", "critical thing", level="critical")

    summary = greeting.startup_summary()

    assert "critical thing" in summary
    assert "urgent thing" not in summary
    assert "1 more noteworthy thing held" in summary


def test_outside_quiet_hours_urgent_is_spoken_in_full(monkeypatch):
    monkeypatch.setattr(greeting, "in_quiet_hours", lambda _window: False)
    notices.add_notice("t", "urgent thing", level="urgent")

    assert "urgent thing" in greeting.startup_summary()


def test_normal_notices_are_only_ever_counted(monkeypatch):
    monkeypatch.setattr(greeting, "in_quiet_hours", lambda _window: False)
    notices.add_notice("t", "a calm detail nobody asked for", level="normal")

    summary = greeting.startup_summary()

    assert "a calm detail nobody asked for" not in summary
    assert "1 quieter note" in summary
