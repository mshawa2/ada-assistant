"""The calendar tools.

Nothing here touches the real calendar: `_event_store` is replaced with a fake
that mimics the handful of EventKit methods these tools call. That keeps the
suite from creating events on the developer's actual calendar — the same class
of mistake the notification tests made before they were isolated — while still
exercising all the logic that isn't Apple's.
"""

import sys
import time
from datetime import datetime, timedelta

import pytest

from ada import when
from ada.tools import calendar_tool


def _iso(**delta):
    return (datetime.now() + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S")


class FakeDate:
    def __init__(self, epoch):
        self._epoch = epoch

    def timeIntervalSince1970(self):
        return self._epoch


class FakeCalendar:
    def __init__(self, title, writable=True):
        self._title = title
        self._writable = writable

    def title(self):
        return self._title

    def allowsContentModifications(self):
        return self._writable


class FakeEvent:
    def __init__(self, title, start, end=None, calendar=None, all_day=False, location=None, notes=None):
        self._title = title
        self._start = start
        self._end = end if end is not None else start + 3600
        self._calendar = calendar or FakeCalendar("Home")
        self._all_day = all_day
        self._location = location
        self._notes = notes

    @property
    def identifier(self):
        return f"evt-{self._title.lower().replace(' ', '-')}"

    def title(self):
        return self._title

    def startDate(self):
        return FakeDate(self._start)

    def endDate(self):
        return FakeDate(self._end)

    def calendar(self):
        return self._calendar

    def isAllDay(self):
        return self._all_day

    def location(self):
        return self._location

    def notes(self):
        return self._notes

    def eventIdentifier(self):
        return self.identifier

    # Setters used by create_event.
    def setTitle_(self, v):
        self._title = v

    def setStartDate_(self, v):
        self._start = v.timeIntervalSince1970()

    def setEndDate_(self, v):
        self._end = v.timeIntervalSince1970()

    def setAllDay_(self, v):
        self._all_day = v

    def setCalendar_(self, v):
        self._calendar = v

    def setLocation_(self, v):
        self._location = v

    def setNotes_(self, v):
        self._notes = v


class FakeStore:
    def __init__(self, default="Home"):
        self.events = []
        self.calendars = [
            FakeCalendar("Home"),
            FakeCalendar("Work"),
            FakeCalendar("US Holidays", writable=False),
        ]
        self._default = next((c for c in self.calendars if c.title() == default), None)
        self.save_ok = True
        self.removed = []

    def calendarsForEntityType_(self, _entity):
        return self.calendars

    def defaultCalendarForNewEvents(self):
        return self._default

    def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, _cals):
        return (start.timeIntervalSince1970(), end.timeIntervalSince1970())

    def eventsMatchingPredicate_(self, predicate):
        start, end = predicate
        return [e for e in self.events if start <= e._start <= end]

    def eventWithIdentifier_(self, identifier):
        return next((e for e in self.events if e.identifier == identifier), None)

    def saveEvent_span_error_(self, event, _span, _error):
        if not self.save_ok:
            return False, "calendar is read-only"
        self.events.append(event)
        return True, None

    def removeEvent_span_error_(self, event, _span, _error):
        self.events.remove(event)
        self.removed.append(event)
        return True, None


class FakeEventKit:
    EKEntityTypeEvent = 0
    EKSpanThisEvent = 0

    class EKEvent:
        @staticmethod
        def eventWithEventStore_(_store):
            return FakeEvent("", 0)


@pytest.fixture
def store(monkeypatch):
    """Install a fake event store and the EventKit shim the tools import."""
    fake = FakeStore()
    monkeypatch.setattr(calendar_tool, "_event_store", lambda: fake)
    monkeypatch.setattr(calendar_tool, "_epoch_to_nsdate", FakeDate)
    monkeypatch.setitem(sys.modules, "EventKit", FakeEventKit)
    return fake


# --- reading ----------------------------------------------------------------


def test_an_empty_calendar_says_so_plainly(store):
    assert "Nothing on the calendar" in calendar_tool.list_events()


def test_events_are_grouped_by_day_with_times_and_ids(store):
    start = time.time() + 3600
    store.events.append(FakeEvent("Dentist", start, location="Baker St"))

    listed = calendar_tool.list_events(days_ahead=7)

    assert when.day(start) in listed
    assert "Dentist" in listed
    assert "@ Baker St" in listed
    assert "id=evt-dentist" in listed, "the id must be there for delete_event to use"


def test_all_day_events_say_all_day_rather_than_a_time(store):
    store.events.append(FakeEvent("Bank holiday", time.time() + 7200, all_day=True))

    assert "all day" in calendar_tool.list_events()


def test_events_come_back_in_time_order(store):
    now = time.time()
    store.events.append(FakeEvent("Later", now + 7200))
    store.events.append(FakeEvent("Sooner", now + 600))

    listed = calendar_tool.list_events()

    assert listed.index("Sooner") < listed.index("Later")


def test_events_outside_the_window_are_not_returned(store):
    store.events.append(FakeEvent("Next month", time.time() + 30 * 86400))

    assert "Nothing on the calendar" in calendar_tool.list_events(days_ahead=7)
    assert "Next month" in calendar_tool.list_events(days_ahead=60)


def test_the_window_is_clamped_to_something_sane(store):
    """A model asking for 10,000 days shouldn't produce a decade-long scan."""
    calendar_tool.list_events(days_ahead=99999)
    calendar_tool.list_events(days_ahead=-5)


def test_listing_can_start_from_a_given_day(store):
    store.events.append(FakeEvent("Friday thing", time.time() + 3 * 86400))

    assert "Friday thing" in calendar_tool.list_events(days_ahead=1, start_at=_iso(days=3, hours=-1))


# --- searching --------------------------------------------------------------


def test_find_matches_on_title(store):
    store.events.append(FakeEvent("Dentist appointment", time.time() + 86400))

    assert "Dentist" in calendar_tool.find_events("dentist")


def test_find_matches_on_location_and_notes(store):
    store.events.append(FakeEvent("Catch up", time.time() + 86400, location="The Ivy"))
    store.events.append(FakeEvent("Standup", time.time() + 86400, notes="with Sarah"))

    assert "Catch up" in calendar_tool.find_events("ivy")
    assert "Standup" in calendar_tool.find_events("sarah")


def test_find_is_case_insensitive(store):
    store.events.append(FakeEvent("Dentist", time.time() + 86400))

    assert "Dentist" in calendar_tool.find_events("DENTIST")


def test_find_reports_nothing_rather_than_inventing_a_match(store):
    store.events.append(FakeEvent("Dentist", time.time() + 86400))

    assert "Nothing on the calendar matching" in calendar_tool.find_events("opera")


# --- calendars --------------------------------------------------------------


def test_listing_calendars_marks_default_and_read_only(store):
    listed = calendar_tool.list_calendars()

    assert "Home (default, writable)" in listed
    assert "US Holidays (read-only)" in listed


def test_picking_a_named_calendar_is_case_insensitive(store):
    assert calendar_tool._pick_calendar(store, "work").title() == "Work"


def test_picking_a_read_only_calendar_is_refused(store):
    with pytest.raises(ValueError, match="read-only"):
        calendar_tool._pick_calendar(store, "US Holidays")


def test_picking_an_unknown_calendar_lists_the_real_ones(store):
    with pytest.raises(ValueError, match="Writable ones"):
        calendar_tool._pick_calendar(store, "Nonexistent")


# --- creating ---------------------------------------------------------------


def test_creating_an_event_puts_it_on_the_default_calendar(store):
    result = calendar_tool.create_event("Lunch with Sam", _iso(days=1))

    assert len(store.events) == 1
    assert store.events[0].title() == "Lunch with Sam"
    assert store.events[0].calendar().title() == "Home"
    assert "Lunch with Sam" in result


def test_an_event_defaults_to_one_hour(store):
    calendar_tool.create_event("Standup", _iso(days=1))

    event = store.events[0]
    assert event.endDate().timeIntervalSince1970() - event.startDate().timeIntervalSince1970() == 3600


def test_an_explicit_end_time_is_respected(store):
    calendar_tool.create_event("Workshop", _iso(days=1), end_at=_iso(days=1, hours=3))

    event = store.events[0]
    duration = event.endDate().timeIntervalSince1970() - event.startDate().timeIntervalSince1970()
    assert 3 * 3600 - 2 < duration < 3 * 3600 + 2


def test_an_event_that_ends_before_it_starts_is_refused(store):
    with pytest.raises(ValueError, match="end before it starts"):
        calendar_tool.create_event("Backwards", _iso(days=1, hours=3), end_at=_iso(days=1))

    assert store.events == [], "nothing is written when the times are rejected"


def test_an_unparseable_time_is_a_clear_error(store):
    with pytest.raises(ValueError, match="ISO 8601"):
        calendar_tool.create_event("Whenever", "next tuesday-ish")

    assert store.events == []


def test_optional_details_are_carried_through(store):
    calendar_tool.create_event(
        "Dinner", _iso(days=1), location="The Ivy", notes="booked under M", calendar_name="Work"
    )

    event = store.events[0]
    assert event.location() == "The Ivy"
    assert event.notes() == "booked under M"
    assert event.calendar().title() == "Work"


def test_a_failed_save_is_reported_not_swallowed(store):
    store.save_ok = False

    with pytest.raises(ValueError, match="Couldn't save"):
        calendar_tool.create_event("Doomed", _iso(days=1))


# --- deleting ---------------------------------------------------------------


def test_deleting_removes_the_event_and_names_it(store):
    store.events.append(FakeEvent("Cancelled meeting", time.time() + 86400))

    result = calendar_tool.delete_event("evt-cancelled-meeting")

    assert store.events == []
    assert "Cancelled meeting" in result


def test_deleting_something_already_gone_is_a_clear_error(store):
    with pytest.raises(ValueError, match="may already be gone"):
        calendar_tool.delete_event("evt-does-not-exist")


# --- permission -------------------------------------------------------------


def test_a_missing_permission_explains_how_to_fix_it(monkeypatch):
    """This has to be actionable, like the messages tool's Full Disk Access
    message — the user is the only one who can grant it."""
    monkeypatch.setattr(calendar_tool, "_store", None)
    monkeypatch.setattr(calendar_tool, "_denied_reason", calendar_tool._PERMISSION_HELP)

    with pytest.raises(calendar_tool.CalendarUnavailable, match="System Settings"):
        calendar_tool._event_store()


def test_the_calendar_tools_are_registered():
    from ada.tools import specs

    names = {spec["name"] for spec in specs()}
    assert {"list_events", "find_events", "create_event", "delete_event", "list_calendars"} <= names


def test_calendar_writes_are_gated_by_default():
    """Putting something on a shared calendar is exactly what the rails are for."""
    from ada import safety

    assert safety.requires_confirmation("create_event")
    assert safety.requires_confirmation("delete_event")
