"""The daily brief: what it says, what it stays quiet about, and — the part
that actually matters — what it does when a section is broken."""

import time
from datetime import datetime, timedelta

import pytest

from ada import brief, settings
from ada.heartbeat import notices
from ada.tools import reminders


class FakeEvent:
    """The three EventKit accessors `brief._calendar` actually touches."""

    def __init__(self, title, start):
        self._title = title
        self._start = start

    def title(self):
        return self._title

    def startDate(self):
        return self

    def timeIntervalSince1970(self):
        return self._start


@pytest.fixture(autouse=True)
def no_calendar_and_no_weather(monkeypatch):
    """Neutral defaults, so each test opts into exactly the section it's about.

    The real versions talk to EventKit and to Open-Meteo. A test suite that
    reaches either is a test suite that fails on a machine with no Calendar
    permission and no network.
    """
    monkeypatch.setattr(brief.calendar_tool, "_events_between", lambda start, end: [])
    monkeypatch.setattr(brief.weather, "get_weather", lambda location: f"fine in {location}")


def _iso(**delta):
    return (datetime.now() + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S")


# --- quiet when there's nothing ---------------------------------------------


def test_an_empty_day_says_so_plainly_rather_than_printing_empty_headings():
    assert brief.compose() == brief.NOTHING


def test_a_section_with_nothing_in_it_contributes_no_lines():
    reminders.add_reminder("water the plants", due="in an hour", due_at=_iso(hours=1))

    result = brief.compose()

    assert "Due later today" in result
    assert "Overdue" not in result, "an empty section must not print its heading"
    assert "Waiting on you" not in result


# --- what leads -------------------------------------------------------------


def test_blocked_work_comes_before_everything_else(monkeypatch):
    """A job stopped waiting on you is the only item in the brief that is
    costing you something by being read second."""
    monkeypatch.setattr(
        brief.approvals, "pending", lambda: [{"id": 7, "pending_approval": {"description": "buy the thing"}}]
    )
    monkeypatch.setattr(brief.approvals, "describe", lambda job: "buy the thing")
    reminders.add_reminder("call the dentist", due="an hour ago", due_at=_iso(hours=-1))

    result = brief.compose()

    assert result.index("Waiting on you") < result.index("Overdue")
    assert "Job #7: buy the thing" in result


def test_an_approval_is_not_reported_twice(monkeypatch):
    """`park` writes a notice *and* blocks the job. Both sections can see it;
    only the blocked one should say it."""
    monkeypatch.setattr(brief.approvals, "pending", lambda: [{"id": 3, "pending_approval": {}}])
    monkeypatch.setattr(brief.approvals, "describe", lambda job: "send the email")
    notices.add_notice(brief.approvals.NOTICE_CHECK, "Job #3 needs your approval", level="urgent")
    notices.add_notice("sync", "the portfolio sync failed", level="normal")

    result = brief.compose()

    assert result.count("Job #3") == 1
    assert "the portfolio sync failed" in result, "other notices still come through"


# --- the calendar -----------------------------------------------------------


def test_the_calendar_window_starts_now_not_at_midnight(monkeypatch):
    """A brief read at two in the afternoon that leads with the nine o'clock
    standup is reporting history."""
    seen = {}

    def record(start, end):
        seen["start"], seen["end"] = start, end
        return []

    monkeypatch.setattr(brief.calendar_tool, "_events_between", record)

    brief.compose()

    assert seen["start"] == pytest.approx(time.time(), abs=5)
    assert seen["end"] == pytest.approx(brief._end_of_today())


def test_todays_events_are_listed_with_their_times(monkeypatch):
    start = time.time() + 3600
    monkeypatch.setattr(brief.calendar_tool, "_events_between", lambda s, e: [FakeEvent("standup", start)])

    result = brief.compose()

    assert "Left on the calendar today:" in result
    assert "standup" in result


# --- reminders --------------------------------------------------------------


def test_overdue_and_upcoming_are_separated():
    reminders.add_reminder("renew the passport", due="yesterday", due_at=_iso(days=-1))
    reminders.add_reminder("collect the parcel", due="later", due_at=_iso(hours=2))

    result = brief.compose()

    assert "Overdue (1):" in result
    assert "renew the passport" in result
    assert result.index("renew the passport") < result.index("collect the parcel")


def test_a_reminder_due_tomorrow_is_not_in_todays_brief():
    reminders.add_reminder("dentist", due="tomorrow", due_at=_iso(days=1))

    assert brief.compose() == brief.NOTHING


def test_undated_reminders_are_counted_not_listed():
    """A standing backlog of someday-items listed under a heading about right
    now is how a brief becomes something you skim past."""
    reminders.add_reminder("fix the gate")
    reminders.add_reminder("think about the garden")
    reminders.add_reminder("call the dentist", due="an hour ago", due_at=_iso(hours=-1))

    result = brief.compose()

    assert "2 other reminders with no time set" in result
    assert "fix the gate" not in result


def test_undated_reminders_alone_are_not_news():
    """On their own they aren't a report about today, just the length of a list."""
    reminders.add_reminder("fix the gate")

    assert brief.compose() == brief.NOTHING


def test_a_completed_reminder_is_not_overdue():
    reminders.add_reminder("call the dentist", due="an hour ago", due_at=_iso(hours=-1))
    reminders.complete_reminder(1)

    assert brief.compose() == brief.NOTHING


# --- the weather ------------------------------------------------------------


def test_weather_is_skipped_when_no_location_is_configured():
    """Guessing it from the machine's IP sends an address to a third party
    nobody asked about, and is wrong on a VPN besides."""
    assert brief.compose() == brief.NOTHING


def test_weather_is_included_once_a_location_is_set():
    settings.set_path("locale.location", "Austin, TX")

    assert "fine in Austin, TX" in brief.compose()


# --- the part that matters --------------------------------------------------


def test_a_broken_section_is_reported_not_silently_dropped(monkeypatch):
    """The whole reason this is not a bare try/except pass.

    If the calendar can't be read, omitting it renders as "you have nothing on
    today" — a brief confidently telling you your morning is free is worse than
    no brief at all.
    """
    monkeypatch.setattr(
        brief.calendar_tool,
        "_events_between",
        lambda s, e: (_ for _ in ()).throw(RuntimeError("no Calendar permission")),
    )

    result = brief.compose()

    assert "couldn't read the calendar" in result
    assert "no Calendar permission" in result


def test_one_broken_section_does_not_take_the_others_down(monkeypatch):
    monkeypatch.setattr(
        brief.calendar_tool, "_events_between", lambda s, e: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    reminders.add_reminder("call the dentist", due="an hour ago", due_at=_iso(hours=-1))

    result = brief.compose()

    assert "couldn't read the calendar" in result
    assert "call the dentist" in result, "the surviving sections still run"


def test_compose_never_raises(monkeypatch):
    """The brief is called from a tool and from the greeting path. A section
    that throws must not become an error the user sees instead of their day."""

    def explode():
        raise RuntimeError("everything is broken")

    monkeypatch.setattr(brief, "SECTIONS", tuple((name, explode) for name, _ in brief.SECTIONS))

    result = brief.compose()

    assert result.count("everything is broken") == len(brief.SECTIONS)
