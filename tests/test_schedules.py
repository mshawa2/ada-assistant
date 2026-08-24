"""Recurring work: the arithmetic, the store, and the tier that fires it.

The arithmetic gets the most attention because it's the part with no feedback
loop — a schedule that computes the wrong next run is silently wrong for a day
before anyone notices, and then only if they were expecting it.
"""

import time
from datetime import datetime, timedelta

import pytest

from ada import jobs, schedules, when
from ada.heartbeat import scheduler
from ada.tools import schedules_tool


def _local(**replace):
    """A local datetime a fixed distance from now, for readable assertions."""
    return when.local(time.time()).replace(second=0, microsecond=0, **replace)


# --- validating the spec ----------------------------------------------------


def test_a_good_spec_comes_back_normalised():
    assert schedules.validate({"repeat": "DAILY", "at": "8:5"}) == {"repeat": "daily", "at": "08:05"}


def test_an_unknown_repeat_names_the_ones_that_work():
    with pytest.raises(ValueError, match="hourly"):
        schedules.validate({"repeat": "fortnightly"})


def test_a_weekly_schedule_needs_a_real_day():
    assert schedules.validate({"repeat": "weekly", "on": "Monday", "at": "08:00"})["on"] == "monday"

    with pytest.raises(ValueError, match="day of the week"):
        schedules.validate({"repeat": "weekly", "on": "someday", "at": "08:00"})


def test_a_monthly_schedule_cannot_ask_for_a_day_february_lacks():
    """"The nearest real day" is a guess about what someone meant."""
    assert schedules.validate({"repeat": "monthly", "on": "15", "at": "09:00"})["on"] == 15

    with pytest.raises(ValueError, match="February is why"):
        schedules.validate({"repeat": "monthly", "on": "30", "at": "09:00"})


def test_a_schedule_cannot_be_faster_than_a_loop():
    with pytest.raises(ValueError, match="too often"):
        schedules.validate({"repeat": "every_seconds", "every_seconds": 5})


def test_a_time_of_day_that_is_not_one_is_rejected():
    for bad in ("25:00", "half eight", "08:99"):
        with pytest.raises(ValueError, match="time of day"):
            schedules.validate({"repeat": "daily", "at": bad})


# --- when it next runs ------------------------------------------------------


def test_daily_lands_on_the_next_occurrence_of_that_time():
    spec = {"repeat": "daily", "at": "08:00"}
    reference = _local(hour=9, minute=0).timestamp()

    following = when.local(schedules.next_run(spec, after=reference))

    assert (following.hour, following.minute) == (8, 0)
    assert following.date() == (when.local(reference) + timedelta(days=1)).date(), "9am has passed today"


def test_daily_still_fires_today_if_the_time_has_not_come():
    spec = {"repeat": "daily", "at": "23:30"}
    reference = _local(hour=6, minute=0).timestamp()

    assert when.local(schedules.next_run(spec, after=reference)).date() == when.local(reference).date()


def test_weekly_lands_on_the_right_weekday():
    spec = {"repeat": "weekly", "on": "wednesday", "at": "08:00"}

    following = when.local(schedules.next_run(spec, after=time.time()))

    assert following.strftime("%A").lower() == "wednesday"
    assert (following.hour, following.minute) == (8, 0)
    assert following.timestamp() > time.time()


def test_monthly_lands_on_the_right_day_of_the_month():
    spec = {"repeat": "monthly", "on": 15, "at": "09:00"}

    following = when.local(schedules.next_run(spec, after=time.time()))

    assert following.day == 15
    assert following.timestamp() > time.time()


def test_the_next_run_is_always_strictly_later():
    """Otherwise firing a schedule and asking for its next run hands back the
    moment that just passed, and it fires again on the same tick."""
    exactly_now = _local(hour=8, minute=0).timestamp()

    assert schedules.next_run({"repeat": "daily", "at": "08:00"}, after=exactly_now) > exactly_now
    assert schedules.next_run({"repeat": "hourly"}, after=exactly_now) > exactly_now
    assert schedules.next_run({"repeat": "every_seconds", "every_seconds": 60}, after=exactly_now) > exactly_now


def test_how_a_schedule_reads_out_loud():
    assert schedules.describe({"repeat": "daily", "at": "08:00"}) == "every day at 08:00"
    assert schedules.describe({"repeat": "weekly", "on": "sunday", "at": "18:00"}) == "every Sunday at 18:00"
    assert "1st of each month" in schedules.describe({"repeat": "monthly", "on": 1, "at": "09:00"})
    assert "2nd" in schedules.describe({"repeat": "monthly", "on": 2, "at": "09:00"})
    assert "11th" in schedules.describe({"repeat": "monthly", "on": 11, "at": "09:00"})
    assert schedules.describe({"repeat": "every_seconds", "every_seconds": 3600}) == "every hour"


# --- the store --------------------------------------------------------------


def test_a_new_schedule_is_armed_for_its_first_run():
    schedule = schedules.create("morning briefing", {"repeat": "daily", "at": "08:00"})

    assert schedule["enabled"] is True
    assert schedule["runs"] == 0
    assert schedule["next_due_at"] > time.time()


def test_a_schedule_with_no_task_is_rejected():
    with pytest.raises(ValueError, match="needs a task"):
        schedules.create("  ", {"repeat": "daily", "at": "08:00"})

    assert schedules.load_schedules() == []


def test_a_bad_spec_is_caught_at_creation_not_at_the_first_missed_run():
    with pytest.raises(ValueError):
        schedules.create("x", {"repeat": "weekly", "on": "someday"})

    assert schedules.load_schedules() == []


def test_only_schedules_whose_time_has_come_are_due():
    schedules.create("soon", {"repeat": "daily", "at": "08:00"})
    schedules.create("also soon", {"repeat": "hourly"})

    assert schedules.due() == []
    assert len(schedules.due(now=time.time() + 86_400 * 2)) == 2


def test_a_paused_schedule_is_never_due():
    schedules.create("paused one", {"repeat": "hourly"})
    schedules.set_enabled(1, False)

    assert schedules.due(now=time.time() + 86_400) == []


def test_resuming_re_arms_from_now_rather_than_firing_everything_missed():
    schedules.create("x", {"repeat": "hourly"})
    schedules.set_enabled(1, False)

    resumed = schedules.set_enabled(1, True)

    assert resumed["next_due_at"] > time.time()
    assert schedules.due() == []


def test_firing_records_the_run_and_arms_the_next_one():
    schedules.create("briefing", {"repeat": "daily", "at": "08:00"})
    was_due = schedules.get(1)["next_due_at"]

    # Fired at the moment it came due, which is the only way run_schedules
    # ever calls this — `due()` won't hand back anything earlier.
    fired = schedules.mark_fired(1, job_id=7, now=was_due)

    assert fired["runs"] == 1
    assert fired["last_job_id"] == 7
    assert fired["next_due_at"] > was_due, "armed for next time, not stuck on this one"


def test_a_missed_run_fires_once_and_resumes_its_rhythm():
    """A laptop opened after a week away shouldn't produce seven identical
    briefings."""
    schedules.create("briefing", {"repeat": "daily", "at": "08:00"})
    a_week_later = time.time() + 86_400 * 7

    schedules.mark_fired(1, job_id=1, now=a_week_later)

    assert schedules.due(now=a_week_later) == [], "one run, not seven"
    assert schedules.get(1)["next_due_at"] > a_week_later


def test_deleting_a_schedule_removes_it():
    schedules.create("x", {"repeat": "hourly"})
    schedules.delete(1)

    assert schedules.load_schedules() == []
    with pytest.raises(ValueError, match="No schedule with id 1"):
        schedules.require(1)


# --- the check that fires them ----------------------------------------------


def test_a_due_schedule_becomes_a_real_job():
    schedules.create("morning briefing", {"repeat": "daily", "at": "08:00"})
    schedules.mark_fired(1, job_id=0, now=time.time() - 86_400)  # force it due

    scheduler.run_schedules({})

    queued = jobs.load_jobs()
    assert [j["task"] for j in queued] == ["morning briefing"]
    assert queued[0]["state"] == jobs.PENDING


def test_a_scheduled_job_inherits_the_template_s_limits():
    """The whole point of firing into a job: least privilege and the budget
    slice come along without this tier knowing they exist."""
    schedules.create("read the news", {"repeat": "hourly"}, tools=["web_search"], budget_usd=0.10)
    schedules.mark_fired(1, job_id=0, now=time.time() - 7200)

    scheduler.run_schedules({})

    assert jobs.load_jobs()[0]["tools"] == ["web_search"]
    assert jobs.load_jobs()[0]["budget_usd"] == 0.10


def test_nothing_due_creates_nothing():
    schedules.create("later", {"repeat": "daily", "at": "08:00"})

    scheduler.run_schedules({})

    assert jobs.load_jobs() == []


def test_firing_arms_the_schedule_so_it_does_not_fire_again_next_tick():
    schedules.create("hourly thing", {"repeat": "hourly"})
    schedules.mark_fired(1, job_id=0, now=time.time() - 7200)

    scheduler.run_schedules({})
    scheduler.run_schedules({})

    assert len(jobs.load_jobs()) == 1


def test_the_scheduler_is_registered_as_a_heartbeat_check():
    from ada.heartbeat.checks import CHECKS

    assert CHECKS["run_schedules"] is scheduler.run_schedules


# --- the tools --------------------------------------------------------------


def test_scheduling_says_when_it_first_runs():
    result = schedules_tool.schedule_job("morning briefing", "daily", at="08:00")

    assert "#1" in result
    assert "every day at 08:00" in result
    assert "First run" in result


def test_scheduling_rejects_a_spec_it_cannot_honour():
    with pytest.raises(ValueError, match="day of the week"):
        schedules_tool.schedule_job("x", "weekly", on="someday", at="08:00")

    assert schedules.load_schedules() == []


def test_listing_shows_the_rhythm_and_puts_paused_ones_last():
    schedules_tool.schedule_job("briefing", "daily", at="08:00")
    schedules_tool.schedule_job("review", "weekly", on="sunday", at="18:00")
    schedules_tool.manage_schedule(1, "pause")

    listed = schedules_tool.list_schedules()

    assert "every Sunday at 18:00" in listed
    assert "(paused)" in listed
    assert listed.index("review") < listed.index("briefing"), "paused ones sink"


def test_listing_nothing():
    assert "Nothing is scheduled" in schedules_tool.list_schedules()


def test_the_switch_has_three_positions():
    schedules_tool.schedule_job("x", "hourly")

    assert "Paused" in schedules_tool.manage_schedule(1, "pause")
    assert schedules.get(1)["enabled"] is False

    assert "Resumed" in schedules_tool.manage_schedule(1, "resume")
    assert schedules.get(1)["enabled"] is True

    assert "won't run again" in schedules_tool.manage_schedule(1, "cancel")
    assert schedules.load_schedules() == []


def test_an_action_that_is_not_one_is_a_clear_error():
    schedules_tool.schedule_job("x", "hourly")

    with pytest.raises(ValueError, match="isn't a schedule action"):
        schedules_tool.manage_schedule(1, "delete")


def test_managing_a_schedule_that_does_not_exist():
    with pytest.raises(ValueError, match="No schedule with id 9"):
        schedules_tool.manage_schedule(9, "pause")


def test_the_schedule_tools_are_registered():
    from ada.tools import specs

    names = {spec["name"] for spec in specs()}
    assert {"schedule_job", "list_schedules", "manage_schedule"} <= names
