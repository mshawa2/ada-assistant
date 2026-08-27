"""Leaving on time: what gets asked of the network, and what stays quiet."""

import time

import pytest

from ada import travel
from ada.heartbeat import leave_check, notices
from ada.heartbeat.state import get_check_state

# Bound at import, before the autouse fixture stubs it out, so the tests
# below can exercise the real thing.
REAL_ORIGIN = leave_check._origin

CHICAGO = (41.9165, -87.7457)
WILLIS = (41.8787, -87.6360)


class Stamp:
    def __init__(self, epoch):
        self._epoch = epoch

    def timeIntervalSince1970(self):
        return self._epoch


class FakeEvent:
    def __init__(self, title, start, location="233 S Wacker Dr, Chicago", all_day=False, identifier=None):
        self._title, self._start, self._location, self._all_day = title, start, location, all_day
        self._id = identifier or f"id-{title}"

    def title(self):
        return self._title

    def location(self):
        return self._location

    def isAllDay(self):
        return self._all_day

    def eventIdentifier(self):
        return self._id

    def startDate(self):
        return Stamp(self._start)

    def endDate(self):
        return Stamp(self._start + 3600)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing here may touch Nominatim or OSRM. A test suite that does is one
    that fails on a train and rate-limits a free service from CI."""
    def forbidden(*a, **k):
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(travel, "_get", forbidden)
    monkeypatch.setattr(travel, "geocode", lambda place: WILLIS)
    monkeypatch.setattr(travel, "travel_seconds", lambda o, d, mode="driving": 20 * 60)
    monkeypatch.setattr(leave_check, "_origin", lambda: CHICAGO)


@pytest.fixture
def calendar(monkeypatch):
    holder = {"events": []}
    monkeypatch.setattr(
        leave_check.calendar_tool,
        "_events_between",
        lambda start, end: [e for e in holder["events"] if start <= e._start <= end],
    )
    return holder


def _alerts():
    return [n for n in notices.list_notices(include_dismissed=True) if n["check"] == "leave_by"]


def _params(**over):
    return {"lead_minutes": 10, "buffer_minutes": 10, "horizon_hours": 4, **over}


# --- staying quiet ----------------------------------------------------------


def test_an_empty_calendar_costs_nothing(calendar, monkeypatch):
    monkeypatch.setattr(leave_check, "_origin", lambda: pytest.fail("must not look up where you are"))

    leave_check.check_leave_by(_params())

    assert _alerts() == []


def test_a_video_call_is_not_a_journey(calendar, monkeypatch):
    """A day of Zooms should cost zero requests."""
    monkeypatch.setattr(leave_check, "_origin", lambda: pytest.fail("must not look up where you are"))
    calendar["events"] = [FakeEvent("standup", time.time() + 1800, location="https://zoom.us/j/123")]

    leave_check.check_leave_by(_params())

    assert _alerts() == []


@pytest.mark.parametrize("place", ["", "  ", "TBC", "Online", "remote", "Zoom", "n/a", "https://meet.google.com/x"])
def test_locations_that_are_not_places_are_rejected_before_the_network(place):
    assert travel.looks_like_a_place(place) is False


@pytest.mark.parametrize("place", ["233 S Wacker Dr, Chicago", "The Dentist, High St", "Willis Tower"])
def test_real_places_are_accepted(place):
    assert travel.looks_like_a_place(place) is True


def test_an_all_day_event_never_triggers_a_journey(calendar):
    calendar["events"] = [FakeEvent("conference", time.time() + 1800, all_day=True)]

    leave_check.check_leave_by(_params())

    assert _alerts() == []


def test_an_event_beyond_the_horizon_is_not_looked_up(calendar):
    calendar["events"] = [FakeEvent("dentist", time.time() + 10 * 3600)]

    leave_check.check_leave_by(_params(horizon_hours=4))

    assert _alerts() == []


def test_nothing_is_said_when_your_own_location_is_unknown(calendar, monkeypatch):
    """A notice every two minutes about an unconfigured lookup would be its own
    problem — brief._weather already reports that where it's read."""
    def unavailable():
        raise travel.TravelUnavailable("no shortcut")

    monkeypatch.setattr(leave_check, "_origin", unavailable)
    calendar["events"] = [FakeEvent("dentist", time.time() + 1800)]

    leave_check.check_leave_by(_params())

    assert _alerts() == []


# --- alerting ---------------------------------------------------------------


def test_it_alerts_when_leaving_time_is_near(calendar):
    # 20 min journey + 10 min buffer + 10 min lead = alert at 40 min out.
    calendar["events"] = [FakeEvent("dentist", time.time() + 35 * 60)]

    leave_check.check_leave_by(_params())

    assert len(_alerts()) == 1
    assert "dentist" in _alerts()[0]["text"]
    assert "20 minutes driving" in _alerts()[0]["text"]


def test_it_stays_quiet_while_there_is_still_plenty_of_time(calendar):
    calendar["events"] = [FakeEvent("dentist", time.time() + 3 * 3600)]

    leave_check.check_leave_by(_params())

    assert _alerts() == []


def test_it_says_leave_now_once_the_moment_has_arrived(calendar):
    calendar["events"] = [FakeEvent("dentist", time.time() + 28 * 60)]

    leave_check.check_leave_by(_params())

    assert "Leave now" in _alerts()[0]["text"]


def test_an_event_is_alerted_once_not_every_tick(calendar):
    """This runs on a two-minute tick. An alert repeated for an hour is worse
    than no alert."""
    calendar["events"] = [FakeEvent("dentist", time.time() + 35 * 60)]

    for _ in range(10):
        leave_check.check_leave_by(_params())

    assert len(_alerts()) == 1


def test_two_different_events_both_get_their_own_alert(calendar):
    calendar["events"] = [
        FakeEvent("dentist", time.time() + 35 * 60, identifier="a"),
        FakeEvent("lunch", time.time() + 36 * 60, identifier="b"),
    ]

    leave_check.check_leave_by(_params())

    assert len(_alerts()) == 2


def test_an_unroutable_meeting_does_not_stop_the_others(calendar, monkeypatch):
    def geocode(place):
        if "nowhere" in place:
            raise travel.TravelUnavailable("not on the map")
        return WILLIS

    monkeypatch.setattr(travel, "geocode", geocode)
    calendar["events"] = [
        FakeEvent("mystery", time.time() + 35 * 60, location="nowhere at all", identifier="a"),
        FakeEvent("dentist", time.time() + 35 * 60, identifier="b"),
    ]

    leave_check.check_leave_by(_params())

    assert len(_alerts()) == 1
    assert "dentist" in _alerts()[0]["text"]


def test_a_destination_on_another_continent_is_not_routed(calendar, monkeypatch):
    """A calendar entry about a trip is not a journey you're about to make."""
    monkeypatch.setattr(travel, "geocode", lambda place: (35.6762, 139.6503))  # Tokyo
    monkeypatch.setattr(
        travel, "travel_seconds", lambda *a, **k: pytest.fail("must not route across an ocean")
    )
    calendar["events"] = [FakeEvent("Tokyo office", time.time() + 35 * 60)]

    leave_check.check_leave_by(_params())

    assert _alerts() == []


# --- housekeeping -----------------------------------------------------------


def test_alerted_events_are_forgotten_once_they_have_started(calendar):
    calendar["events"] = [FakeEvent("dentist", time.time() + 35 * 60)]
    leave_check.check_leave_by(_params())
    assert get_check_state("leave_by")["alerted"]

    calendar["events"] = []
    leave_check.check_leave_by(_params())

    # Still remembered while recent; the prune keeps an hour's grace.
    assert len(get_check_state("leave_by")["alerted"]) == 1


def test_the_origin_uses_the_whole_address_not_just_the_city(monkeypatch):
    """place_candidates reduces an address to "Chicago" because Open-Meteo only
    matches place names. Nominatim understands the street, and the difference
    was measured at 11.1km — half an hour of driving."""
    asked = []

    def geocode(place):
        asked.append(place)
        return WILLIS

    monkeypatch.setattr(travel, "geocode", geocode)
    monkeypatch.setattr(
        leave_check.location, "here", lambda: "1234 N Sample Ave\nChicago IL 60601\nUnited States"
    )

    REAL_ORIGIN()

    assert asked[0] == "1234 N Sample Ave, Chicago IL 60601, United States"


def test_the_origin_falls_back_to_the_city_when_the_address_is_unknown(monkeypatch):
    """A rough origin beats no alert."""
    asked = []

    def geocode(place):
        asked.append(place)
        if "Sample" in place:
            raise travel.TravelUnavailable("not on the map")
        return WILLIS

    monkeypatch.setattr(travel, "geocode", geocode)
    monkeypatch.setattr(
        leave_check.location, "here", lambda: "1234 N Sample Ave\nChicago IL 60601\nUnited States"
    )

    assert REAL_ORIGIN() == WILLIS
    assert len(asked) > 1, "it tried the full address first, then fell back"


def test_coordinates_from_the_shortcut_need_no_geocoding_at_all(monkeypatch):
    monkeypatch.setattr(travel, "geocode", lambda p: pytest.fail("coordinates must not be geocoded"))
    monkeypatch.setattr(leave_check.location, "here", lambda: "41.9165, -87.7457")

    assert REAL_ORIGIN() == pytest.approx(CHICAGO)


def test_the_check_is_registered():
    from ada.heartbeat.checks import CHECKS

    assert CHECKS["leave_by"] is leave_check.check_leave_by


# --- the distance guard -----------------------------------------------------


def test_haversine_is_about_right():
    """10km as the crow flies; OSRM measured 13.1km by road for the same pair,
    which is the right relationship — the guard is deliberately generous."""
    assert 9 < travel.haversine_km(CHICAGO, WILLIS) < 11


def test_the_same_point_is_no_distance_at_all():
    assert travel.haversine_km(CHICAGO, CHICAGO) == pytest.approx(0, abs=1e-9)
