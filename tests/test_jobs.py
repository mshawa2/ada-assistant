"""The job table: lifecycle, atomic claiming, durability, and orphan recovery.

The state machine is the point of this tier, so these lean on transitions
rather than on storage. The two that matter most are `claim` under contention
— a job that runs twice is a message sent twice, once there's an executor —
and reaping, because a laptop closing mid-job is the ordinary case, not the
exceptional one.
"""

import json
import os
import threading
import time

import pytest

from ada import jobs, store
from ada.tools import jobs_tool


def _states():
    return {j["id"]: j["state"] for j in jobs.load_jobs()}


# --- creating ---------------------------------------------------------------


def test_a_new_job_starts_queued_and_gets_the_next_id():
    first = jobs.create("summarise this week's notes")
    second = jobs.create("check the flight price")

    assert (first["id"], second["id"]) == (1, 2)
    assert first["state"] == jobs.PENDING
    assert first["attempts"] == 0
    assert first["owner_pid"] is None


def test_a_job_with_no_task_is_rejected():
    with pytest.raises(ValueError, match="needs a task"):
        jobs.create("   ")

    assert jobs.load_jobs() == []


def test_the_seams_for_later_tiers_are_stored_from_the_start():
    """Written now so adding them later doesn't mean migrating live records."""
    job = jobs.create("x", run_at=time.time() + 60, tools=["web_search"], budget_usd=0.25)

    stored = jobs.get(job["id"])
    assert stored["tools"] == ["web_search"]
    assert stored["budget_usd"] == 0.25
    assert stored["run_at"] is not None
    assert stored["pending_approval"] is None


def test_a_job_survives_a_restart():
    """The record is on disk, not in memory — the whole reason this tier exists."""
    jobs.create("still here tomorrow")

    on_disk = json.loads((store.DATA_DIR / "jobs.json").read_text())

    assert on_disk[0]["task"] == "still here tomorrow"
    assert on_disk[0]["state"] == jobs.PENDING


# --- claiming ---------------------------------------------------------------


def test_claiming_marks_the_job_running_and_records_the_owner():
    jobs.create("first")

    claimed = jobs.claim()

    assert claimed["state"] == jobs.RUNNING
    assert claimed["owner_pid"] == os.getpid()
    assert claimed["attempts"] == 1
    assert claimed["started_at"] is not None


def test_claiming_takes_the_oldest_first():
    jobs.create("first")
    jobs.create("second")

    assert jobs.claim()["task"] == "first"
    assert jobs.claim()["task"] == "second"


def test_nothing_to_claim_returns_none_rather_than_raising():
    assert jobs.claim() is None


def test_a_job_scheduled_for_later_is_not_claimed_yet():
    jobs.create("not before three", run_at=time.time() + 3600)

    assert jobs.claim() is None, "a future job must stay queued"

    # ...but it is claimable once its time has come.
    assert jobs.claim(now=time.time() + 7200)["task"] == "not before three"


def test_two_workers_racing_never_claim_the_same_job():
    """The invariant the whole module is arranged around.

    A list-then-start implementation has a window where both workers see the
    same pending job. With an executor attached that means the job's tools run
    twice — a message sent twice, a purchase made twice.
    """
    for i in range(20):
        jobs.create(f"job {i}")

    claimed = []
    guard = threading.Lock()
    start = threading.Barrier(8)

    def worker():
        start.wait()
        while True:
            job = jobs.claim()
            if job is None:
                return
            with guard:
                claimed.append(job["id"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(claimed) == list(range(1, 21)), "every job claimed exactly once"
    assert all(state == jobs.RUNNING for state in _states().values())


# --- finishing --------------------------------------------------------------


def test_finishing_records_the_result_and_releases_the_owner():
    jobs.create("x")
    jobs.claim()

    done = jobs.finish(1, "Found three flights under 200.")

    assert done["state"] == jobs.DONE
    assert done["result"] == "Found three flights under 200."
    assert done["owner_pid"] is None
    assert done["finished_at"] is not None


def test_failing_keeps_the_error_for_inspection():
    jobs.create("x")
    jobs.claim()

    failed = jobs.fail(1, "the search provider returned 500")

    assert failed["state"] == jobs.FAILED
    assert failed["error"] == "the search provider returned 500"


def test_a_failed_job_can_be_put_back_on_the_queue():
    jobs.create("x")
    jobs.claim()
    jobs.fail(1, "flaky network")

    requeued = jobs.retry(1)

    assert requeued["state"] == jobs.PENDING
    assert requeued["error"] is None
    assert requeued["attempts"] == 1, "the attempt still counts — retrying isn't forgetting"


# --- the approval seam ------------------------------------------------------


def test_a_job_can_park_waiting_for_a_human():
    jobs.create("email the landlord")
    jobs.claim()

    parked = jobs.park(1, {"tool": "compose_email", "description": "Email Sam: 'the boiler again'"})

    assert parked["state"] == jobs.WAITING_APPROVAL
    assert parked["pending_approval"]["tool"] == "compose_email"
    assert parked["owner_pid"] is None, "nobody is working it while it waits"


def test_approval_puts_the_job_back_on_the_queue_with_a_grant():
    """Not straight to running: parking released the worker, so there is no
    process still sitting in this job waiting to be told to carry on."""
    jobs.create("email the landlord")
    jobs.claim()
    jobs.park(1, {"tool": "compose_email", "input": {"to": "sam"}, "description": "Email Sam"})

    requeued = jobs.resume(1)

    assert requeued["state"] == jobs.PENDING
    assert requeued["owner_pid"] is None
    assert requeued["pending_approval"] is None, "the question is answered"
    assert requeued["granted"]["tool"] == "compose_email", "the answer is kept as a grant"
    assert jobs.claim()["id"] == 1, "and it is claimable again"


def test_a_grant_is_spent_once_and_only_on_the_exact_call():
    jobs.create("x")
    jobs.claim()
    jobs.park(1, {"tool": "send_message", "input": {"to": "sam", "text": "hi"}, "description": "d"})
    jobs.resume(1)

    assert not jobs.consume_grant(1, "send_message", {"to": "sam", "text": "something else"}), (
        "approving one message must not authorise a different one"
    )
    assert not jobs.consume_grant(1, "compose_email", {"to": "sam", "text": "hi"}), (
        "nor a different tool with the same arguments"
    )

    assert jobs.consume_grant(1, "send_message", {"to": "sam", "text": "hi"}) is True
    assert jobs.consume_grant(1, "send_message", {"to": "sam", "text": "hi"}) is False, "one shot"


def test_a_grant_does_not_survive_the_job_ending():
    jobs.create("x")
    jobs.claim()
    jobs.park(1, {"tool": "send_message", "input": {"to": "sam"}, "description": "d"})
    jobs.resume(1)
    jobs.claim()
    jobs.finish(1, "done")

    assert jobs.get(1)["granted"] is None


def test_parking_without_an_action_is_rejected():
    """A job waiting on approval with nothing to approve can never be resolved."""
    jobs.create("x")
    jobs.claim()

    with pytest.raises(ValueError, match="needs the action"):
        jobs.park(1, {})

    assert jobs.get(1)["state"] == jobs.RUNNING


# --- illegal transitions ----------------------------------------------------


def test_a_finished_job_cannot_be_moved():
    jobs.create("x")
    jobs.claim()
    jobs.finish(1, "done")

    for move in (lambda: jobs.cancel(1), lambda: jobs.fail(1, "no"), lambda: jobs.retry(1)):
        with pytest.raises(jobs.JobError, match="it has finished"):
            move()

    assert jobs.get(1)["state"] == jobs.DONE


def test_a_queued_job_cannot_be_finished_without_running():
    jobs.create("x")

    with pytest.raises(jobs.JobError, match="is pending"):
        jobs.finish(1, "pretending")


def test_an_illegal_transition_says_what_is_actually_allowed():
    """It raises rather than no-oping: a job that silently refuses to change
    state is a job you'll believe is running forever."""
    jobs.create("x")

    with pytest.raises(jobs.JobError) as caught:
        jobs.finish(1, "nope")

    assert "running" in str(caught.value) and "cancelled" in str(caught.value)


def test_an_unknown_job_is_a_clear_error():
    with pytest.raises(ValueError, match="No job with id 99"):
        jobs.require(99)


def test_a_job_can_be_cancelled_from_any_open_state():
    for i in range(3):
        jobs.create(f"job {i}")

    jobs.cancel(1)  # from pending

    jobs.claim()  # takes #2
    jobs.park(2, {"tool": "t", "description": "d"})
    jobs.cancel(2)  # from waiting_approval

    jobs.claim()  # takes #3
    jobs.cancel(3)  # from running

    assert set(_states().values()) == {jobs.CANCELLED}


# --- orphans ----------------------------------------------------------------


def test_a_live_process_is_recognised_as_alive():
    assert jobs._process_alive(os.getpid()) is True
    assert jobs._process_alive(None) is False


def test_a_process_that_no_longer_exists_is_recognised_as_dead(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", gone)
    assert jobs._process_alive(4242) is False


def test_a_process_owned_by_someone_else_counts_as_alive(monkeypatch):
    """Treating a live process as dead is what causes the double-run."""

    def not_yours(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", not_yours)
    assert jobs._process_alive(4242) is True


def test_a_job_whose_owner_died_goes_back_on_the_queue(monkeypatch):
    jobs.create("interrupted by a closed laptop")
    jobs.claim()

    monkeypatch.setattr(jobs, "_process_alive", lambda pid: False)
    reaped = jobs.reap_orphans()

    assert [j["id"] for j in reaped] == [1]
    assert jobs.get(1)["state"] == jobs.PENDING
    assert jobs.get(1)["started_at"] is None
    assert jobs.get(1)["attempts"] == 1, "the attempt is remembered so retries stay bounded"


def test_a_job_whose_owner_is_still_alive_is_left_alone():
    jobs.create("genuinely still running")
    jobs.claim()

    assert jobs.reap_orphans() == []
    assert jobs.get(1)["state"] == jobs.RUNNING


def test_reaping_gives_up_after_max_attempts(monkeypatch):
    """Something that takes the process down every time won't succeed on the
    fourth go, and an endless retry loop on a job with real tools is worse
    than an unfinished job."""
    jobs.create("kills the process every time")
    monkeypatch.setattr(jobs, "_process_alive", lambda pid: False)

    for _ in range(jobs.MAX_ATTEMPTS):
        jobs.claim()
        jobs.reap_orphans()

    assert jobs.get(1)["attempts"] == jobs.MAX_ATTEMPTS

    jobs.claim()
    jobs.reap_orphans()

    assert jobs.get(1)["state"] == jobs.FAILED
    assert "died each time" in jobs.get(1)["error"]


def test_reaping_ignores_jobs_that_were_never_running(monkeypatch):
    jobs.create("queued")
    jobs.create("finished")
    jobs.claim()
    jobs.claim()
    jobs.finish(1, "ok")

    monkeypatch.setattr(jobs, "_process_alive", lambda pid: False)
    reaped = jobs.reap_orphans()

    assert [j["id"] for j in reaped] == [2], "only the running one is an orphan"


# --- the tools --------------------------------------------------------------


def test_start_job_does_not_let_ada_claim_she_is_already_working_on_it():
    """The job is queued, not instant — the heartbeat has to come round to it.
    Ada saying "I'm looking into that" the moment it's queued is the same trap
    as a reminder with no due_at."""
    result = jobs_tool.start_job("research train times to Edinburgh")

    assert "#1" in result
    assert "queued" in result
    assert "not that you're working on it now" in result
    assert jobs.load_jobs()[0]["state"] == jobs.PENDING


def test_start_job_accepts_a_time_and_reports_it():
    from datetime import datetime, timedelta

    later = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    result = jobs_tool.start_job("morning summary", run_at=later)

    assert "won't start before" in result
    assert jobs.load_jobs()[0]["run_at"] > time.time()


def test_start_job_rejects_a_time_it_cannot_read():
    with pytest.raises(ValueError, match="ISO 8601"):
        jobs_tool.start_job("x", run_at="tuesday-ish")

    assert jobs.load_jobs() == [], "nothing is queued when the time is rejected"


def test_listing_shows_open_work_only_by_default():
    jobs_tool.start_job("open one")
    jobs_tool.start_job("finished one")
    jobs.claim()
    jobs.claim()
    jobs.finish(2, "all done")

    assert "open one" in jobs_tool.list_jobs()
    assert "finished one" not in jobs_tool.list_jobs()
    assert "finished one" in jobs_tool.list_jobs(include_finished=True)


def test_listing_nothing_open_points_at_the_finished_ones():
    jobs_tool.start_job("x")
    jobs.claim()
    jobs.finish(1, "ok")

    assert "ask to include them" in jobs_tool.list_jobs()


def test_listing_calls_out_jobs_blocked_on_the_user():
    jobs_tool.start_job("email the landlord")
    jobs.claim()
    jobs.park(1, {"tool": "compose_email", "description": "Email Sam about the boiler"})

    listing = jobs_tool.list_jobs()

    assert "waiting on you" in listing
    assert "Email Sam about the boiler" in listing
    assert "blocked waiting for the user to approve" in listing


def test_status_shows_the_whole_history():
    jobs_tool.start_job("x")
    jobs.claim()
    jobs.fail(1, "provider timed out")

    status = jobs_tool.job_status(1)

    assert "created" in status
    assert "claimed by pid" in status
    assert "provider timed out" in status


def test_cancelling_an_already_finished_job_explains_rather_than_raising():
    """The user's intent — that this job doesn't run — is already true."""
    jobs_tool.start_job("x")
    jobs.claim()
    jobs.finish(1, "ok")

    assert "already done" in jobs_tool.cancel_job(1)


def test_cancelling_reports_what_it_stopped():
    jobs_tool.start_job("a long research job")
    jobs.claim()

    assert "running" in jobs_tool.cancel_job(1)
    assert jobs.get(1)["state"] == jobs.CANCELLED


def test_the_job_tools_are_registered_for_the_model():
    from ada.tools import specs

    names = {spec["name"] for spec in specs()}
    assert {"start_job", "list_jobs", "job_status", "cancel_job"} <= names
