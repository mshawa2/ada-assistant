"""The check that turns a schedule into a job.

Deliberately the smallest tier here. It doesn't run anything, doesn't call a
model, and doesn't know what a job does — it notices that a template is due,
creates a job from it, and arms the template for next time. Everything after
that is the executor's, which means a scheduled job gets claiming, the tool
allowlist, the approval gate, its budget slice and orphan recovery without this
module knowing any of them exist.

Firing is cheap (a JSON write), so unlike the executor there's no cap on how
many fire in one tick. Ten due schedules make ten job records; the executor
still runs them one a tick, which is where the real cost is paced.
"""

from ada import audit, jobs, schedules


def run_schedules(params: dict) -> None:
    """The heartbeat check. Materializes anything due into real jobs."""
    for entry in schedules.due():
        job = jobs.create(
            entry["task"],
            tools=entry.get("tools"),
            budget_usd=entry.get("budget_usd"),
        )
        schedules.mark_fired(entry["id"], job["id"])
        audit.log("schedule_fired", schedule=entry["id"], job=job["id"], task=entry["task"])
