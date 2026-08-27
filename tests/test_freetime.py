"""Finding the gaps: the interval arithmetic, and the judgement calls."""

from datetime import timedelta

import pytest

from ada import freetime, settings
from ada.tools import freetime_tool

HOUR = 3600


class Stamp:
    def __init__(self, epoch):
        self._epoch = epoch

    def timeIntervalSince1970(self):
        return self._epoch


class FakeEvent:
    def __init__(self, title, start, end, all_day=False):
        self._title, self._start, self._end, self._all_day = title, start, end, all_day

    def title(self):
        return self._title

    def isAllDay(self):
        return self._all_day

    def startDate(self):
        return Stamp(self._start)

    def endDate(self):
        return Stamp(self._end) if self._end is not None else None


def _day(offset=0):
    midnight = settings.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=offset)


def _at(hour, offset=0):
    return (_day(offset) + timedelta(hours=hour)).timestamp()


# --- the working window -----------------------------------------------------


def test_the_working_day_defaults_to_nine_to_six():
    start, end = freetime.working_window(_day())

    assert (end - start) == 9 * HOUR


def test_the_working_day_is_configurable():
    """Someone else's day starts at six."""
    settings.set_path("locale.day_starts", "06:00")
    settings.set_path("locale.day_ends", "22:00")

    start, end = freetime.working_window(_day())

    assert (end - start) == 16 * HOUR


def test_a_malformed_working_day_is_a_clear_error():
    settings.set_path("locale.day_starts", "half past nine")

    with pytest.raises(ValueError, match="24-hour HH:MM"):
        freetime.working_window(_day())


def test_an_impossible_time_of_day_is_rejected():
    settings.set_path("locale.day_ends", "25:00")

    with pytest.raises(ValueError, match="isn't a real time of day"):
        freetime.working_window(_day())


# --- what counts as busy ----------------------------------------------------


def test_overlapping_events_merge_into_one_busy_span():
    """You can't be more busy than busy."""
    events = [
        FakeEvent("standup", _at(9), _at(10)),
        FakeEvent("review", _at(9.5), _at(11)),
    ]

    assert freetime.busy_intervals(events) == [(_at(9), _at(11))]


def test_touching_events_merge():
    events = [FakeEvent("a", _at(9), _at(10)), FakeEvent("b", _at(10), _at(11))]

    assert freetime.busy_intervals(events) == [(_at(9), _at(11))]


def test_separate_events_stay_separate():
    events = [FakeEvent("a", _at(9), _at(10)), FakeEvent("b", _at(14), _at(15))]

    assert len(freetime.busy_intervals(events)) == 2


def test_an_all_day_event_does_not_make_you_busy():
    """'Sarah's birthday' would otherwise black out the whole day."""
    events = [FakeEvent("Sarah's birthday", _at(0), _at(24), all_day=True)]

    assert freetime.busy_intervals(events) == []


def test_a_zero_length_event_is_not_busy():
    assert freetime.busy_intervals([FakeEvent("ping", _at(9), _at(9))]) == []


# --- the gaps ---------------------------------------------------------------


def test_the_gap_between_two_meetings_is_free():
    events = [FakeEvent("a", _at(10), _at(11)), FakeEvent("b", _at(14), _at(15))]

    slots = freetime.free_slots(events, _at(9), _at(18))

    assert slots == [(_at(9), _at(10)), (_at(11), _at(14)), (_at(15), _at(18))]


def test_an_empty_day_is_one_long_slot():
    assert freetime.free_slots([], _at(9), _at(18)) == [(_at(9), _at(18))]


def test_a_full_day_has_no_slots():
    assert freetime.free_slots([FakeEvent("all of it", _at(9), _at(18))], _at(9), _at(18)) == []


def test_events_outside_the_window_do_not_eat_into_it():
    events = [FakeEvent("breakfast", _at(7), _at(8)), FakeEvent("dinner", _at(20), _at(21))]

    assert freetime.free_slots(events, _at(9), _at(18)) == [(_at(9), _at(18))]


def test_an_event_overhanging_the_window_clips_rather_than_confuses():
    events = [FakeEvent("long meeting", _at(8), _at(11))]

    assert freetime.free_slots(events, _at(9), _at(18)) == [(_at(11), _at(18))]


def test_a_two_minute_crack_between_meetings_is_not_free_time():
    events = [
        FakeEvent("a", _at(9), _at(11)),
        FakeEvent("b", _at(11) + 120, _at(18)),
    ]

    assert freetime.free_slots(events, _at(9), _at(18)) == []


def test_a_backwards_window_is_empty_not_an_error():
    assert freetime.free_slots([], _at(18), _at(9)) == []


# --- the calendar disagreeing with itself -----------------------------------


def test_a_double_booking_is_reported():
    """More useful to hear about than the gap either side of it."""
    a = FakeEvent("dentist", _at(10), _at(11))
    b = FakeEvent("standup", _at(10.5), _at(11.5))

    assert freetime.conflicts([a, b]) == [(a, b)]


def test_back_to_back_is_not_a_conflict():
    a = FakeEvent("a", _at(10), _at(11))
    b = FakeEvent("b", _at(11), _at(12))

    assert freetime.conflicts([a, b]) == []


def test_three_at_once_reports_every_pair():
    events = [
        FakeEvent("a", _at(10), _at(12)),
        FakeEvent("b", _at(10.5), _at(11)),
        FakeEvent("c", _at(10.75), _at(11.5)),
    ]

    assert len(freetime.conflicts(events)) == 3


def test_all_day_events_are_never_conflicts():
    events = [
        FakeEvent("birthday", _at(0), _at(24), all_day=True),
        FakeEvent("standup", _at(10), _at(11)),
    ]

    assert freetime.conflicts(events) == []


# --- the tool ---------------------------------------------------------------


@pytest.fixture
def calendar(monkeypatch):
    """Events returned for any window; the tool clips them itself."""
    holder = {"events": []}
    monkeypatch.setattr(
        freetime_tool.calendar_tool,
        "_events_between",
        lambda start, end: [
            e for e in holder["events"] if e.startDate().timeIntervalSince1970() < end and e._end > start
        ],
    )
    return holder


def test_a_clear_week_reports_free_days(calendar):
    result = freetime_tool.find_free_time(duration_minutes=60, days=3)

    assert result.count("(9h00)") == 3 or "9h00" in result


def test_slots_shorter_than_asked_for_are_left_out(calendar):
    calendar["events"] = [
        FakeEvent("a", _at(9, 1), _at(12, 1)),
        FakeEvent("b", _at(12.5, 1), _at(18, 1)),
    ]

    result = freetime_tool.find_free_time(duration_minutes=60, days=2)

    assert "12:30" not in result, "a 30-minute gap is not an hour"


def test_a_double_booking_leads_the_answer(calendar):
    calendar["events"] = [
        FakeEvent("dentist", _at(10, 1), _at(11, 1)),
        FakeEvent("standup", _at(10.5, 1), _at(11.5, 1)),
    ]

    result = freetime_tool.find_free_time(days=2)

    assert result.startswith("Double-booked:")
    assert "dentist / standup" in result


def test_all_day_events_are_named_rather_than_silently_ignored(calendar):
    calendar["events"] = [FakeEvent("Off sick", _at(0, 1), _at(24, 1), all_day=True)]

    result = freetime_tool.find_free_time(days=2)

    assert "not counted as busy" in result
    assert "Off sick" in result


def test_a_completely_full_period_says_so(calendar):
    calendar["events"] = [FakeEvent("everything", _at(0, 0), _at(24, 30))]

    assert "No free run" in freetime_tool.find_free_time(duration_minutes=60, days=2)


def test_an_event_spanning_two_days_is_not_double_booked_against_itself(calendar):
    """It's returned by both days' queries, and two copies of one meeting look
    exactly like two meetings at once."""
    calendar["events"] = [FakeEvent("conference", _at(10, 1), _at(16, 2))]

    result = freetime_tool.find_free_time(days=4)

    assert "Double-booked" not in result
    assert "conference / conference" not in result


def test_absurd_inputs_are_clamped_not_crashed_on(calendar):
    assert freetime_tool.find_free_time(duration_minutes=0, days=0)
    assert freetime_tool.find_free_time(duration_minutes=99999, days=9999)
