"""Setting up, listing and stopping recurring work.

Three tools rather than five: pause, resume and cancel are one switch with
three positions, and giving each its own slot spends three entries in the
model's tool list on one decision — the same argument that turned
`pause_heartbeat`/`resume_heartbeat`/`heartbeat_status` into one `heartbeat`
tool with an action.

**Not gated, for the same reason `start_job` isn't.** A schedule is inert: it
names work and a rhythm, and does none of it. What matters is the tools each
run calls, and those meet the Tier 6 rails per call in `agent.py` exactly as a
foreground turn does. A recurring job that wants to send a message will stop
and ask every single time — approving one morning's send authorises that one
send, because a grant is matched to the exact arguments and cleared when the
job ends.
"""

from ada import jobs, schedules, when
from ada.tools.registry import tool

_ACTIONS = ("pause", "resume", "cancel")


@tool(
    name="schedule_job",
    description=(
        "Set up work that should happen again and again on a rhythm — a morning "
        "briefing, a Sunday review, an hourly check. Each time it comes round, Ada "
        "creates a background job and works on it without the user.\n\n"
        "Use start_job instead for something that happens once. Use add_reminder "
        "instead to nudge the *user* to do something themselves; this is for work "
        "Ada does.\n\n"
        "Times are the user's local 24-hour clock. Tell them when it will first run — "
        "you can see that in what this returns."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "What to do each time, written so it still makes sense read on its "
                    "own — a run won't have this conversation for context."
                ),
            },
            "repeat": {
                "type": "string",
                "enum": list(schedules.REPEATS),
                "description": "How often: 'hourly', 'daily', 'weekly', 'monthly', or 'every_seconds'.",
            },
            "at": {
                "type": "string",
                "description": "Time of day, 24-hour HH:MM, for daily/weekly/monthly. E.g. '08:00'.",
            },
            "on": {
                "type": "string",
                "description": (
                    "For weekly, the day name ('monday'). For monthly, the day of the "
                    "month as a number 1-28 ('15'). Months don't all have a 29th."
                ),
            },
            "every_seconds": {
                "type": "integer",
                "description": "Only for repeat='every_seconds'. Minimum 60 — each run costs a model call.",
            },
        },
        "required": ["task", "repeat"],
    },
)
def schedule_job(task: str, repeat: str, at: str = "", on: str = "", every_seconds: int = 0) -> str:
    schedule = schedules.create(
        task,
        {"repeat": repeat, "at": at, "on": on, "every_seconds": every_seconds},
    )
    return (
        f"Scheduled #{schedule['id']}: {schedule['task']} — "
        f"{schedules.describe(schedule['spec'])}. "
        f"First run {when.stamp(schedule['next_due_at'])}."
    )


@tool(
    name="list_schedules",
    description=(
        "List the recurring work Ada is set up to do, when each next runs, and whether "
        "any are paused. Use this when the user asks what's scheduled, what runs "
        "automatically, or what Ada does on her own."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_schedules() -> str:
    entries = schedules.load_schedules()
    if not entries:
        return "Nothing is scheduled."

    # Soonest first, with paused ones last — they have no next run to sort by
    # and aren't what the question is usually about.
    entries.sort(key=lambda s: (not s.get("enabled", True), s.get("next_due_at", 0)))
    return "\n".join(schedules.summarize(entry) for entry in entries)


@tool(
    name="manage_schedule",
    description=(
        "Pause, resume, or cancel a recurring schedule by its id. Pausing stops it "
        "running while keeping it, and resuming re-arms it from now rather than "
        "firing everything it missed. Cancelling removes it for good — jobs it has "
        "already created are untouched either way."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The id of the schedule, as shown by list_schedules."},
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "'pause', 'resume', or 'cancel'.",
            },
        },
        "required": ["id", "action"],
    },
)
def manage_schedule(id: int, action: str) -> str:
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"'{action}' isn't a schedule action. Use one of: {', '.join(_ACTIONS)}.")

    schedule = schedules.require(id)

    if action == "cancel":
        schedules.delete(id)
        return f"Cancelled schedule #{id}: {schedule['task']}. It won't run again."

    if action == "pause":
        schedules.set_enabled(id, False)
        return f"Paused schedule #{id}: {schedule['task']}. It stays on the list but won't run."

    resumed = schedules.set_enabled(id, True)
    return (
        f"Resumed schedule #{id}: {resumed['task']} — "
        f"{schedules.describe(resumed['spec'])}. Next run {when.stamp(resumed['next_due_at'])}."
    )
