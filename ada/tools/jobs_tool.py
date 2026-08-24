"""Starting, watching and stopping background work.

Named `jobs_tool` rather than `jobs` for the same reason `calendar_tool` isn't
`calendar`: `ada/jobs.py` is the kernel this wraps, and one of them shadowing
the other on an import is a confusing afternoon.

Jobs run: `ada/heartbeat/executor.py` claims one a tick and gives it a model
turn. What `start_job` returns still avoids over-promising — the job is queued,
not instant, and the heartbeat has to come round to it — because the failure
mode here is Ada reporting she's *looking into* something the moment it's
queued, the same trap as a reminder with no `due_at`.

**Why creating a job isn't gated.** A job record is inert: it names work, it
doesn't do any. What matters is the tools the work later calls, and those are
already gated one layer down in `agent.py`, per call, against the same
autonomy config — a background turn wanting `send_message` will meet exactly
the same rail a foreground turn does. Gating creation as well would ask the
user to approve the *description* of an action and then approve the action,
which trains people to click through both. The rail belongs where the
consequence is. `start_job` is rate-limited instead, so a model stuck in a
loop queues five things and hits a ceiling rather than filling the disk.
"""

from ada import jobs, when
from ada.tools.registry import tool

_OPEN_LABEL = {
    jobs.PENDING: "queued",
    jobs.RUNNING: "running",
    jobs.WAITING_APPROVAL: "waiting on you",
}


@tool(
    name="start_job",
    description=(
        "Queue a piece of background work that should outlive this conversation — "
        "something the user wants done later, or that takes long enough that they "
        "shouldn't sit and wait for it.\n\n"
        "A job is queued, not instant: the background loop picks it up shortly and "
        "works on it without you. Tell the user it's queued and that you'll let them "
        "know — never that you're working on it right now, because you aren't. If "
        "they want something done this minute, just do it here with your normal "
        "tools instead.\n\n"
        "This is not a reminder. Use add_reminder to nudge the user to do something "
        "themselves; use this for work Ada would do."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "What should be done, written so it still makes sense read on its "
                    "own days later — the job won't have this conversation for context."
                ),
            },
            "run_at": {
                "type": "string",
                "description": (
                    "Optional earliest time to run it, ISO 8601 in the user's local "
                    "time, e.g. '2026-08-15T15:00'. Leave out to queue it immediately."
                ),
            },
        },
        "required": ["task"],
    },
)
def start_job(task: str, run_at: str = "") -> str:
    run_epoch = when.to_epoch(run_at) if run_at else None
    job = jobs.create(task, run_at=run_epoch)

    timing = (
        f" It won't start before {when.stamp(run_epoch)}."
        if run_epoch
        else " The background loop will pick it up shortly."
    )
    return (
        f"Queued job #{job['id']}: {job['task']}.{timing} "
        "Tell the user it's queued and that you'll report back — not that you're "
        "working on it now."
    )


@tool(
    name="list_jobs",
    description=(
        "List background jobs and what state they're in. Use this when the user asks "
        "what's running, what's queued, what you're working on, or whether something "
        "they asked for earlier ever happened."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "include_finished": {
                "type": "boolean",
                "description": (
                    "Whether to include jobs that are done, failed or cancelled. "
                    "Defaults to false — only open work."
                ),
            }
        },
        "required": [],
    },
)
def list_jobs(include_finished: bool = False) -> str:
    all_jobs = jobs.load_jobs()
    shown = all_jobs if include_finished else [j for j in all_jobs if j["state"] in jobs.OPEN]

    if not shown:
        if all_jobs and not include_finished:
            return "No open jobs. There are finished ones — ask to include them to see those."
        return "No jobs."

    # Open work first and oldest-first within that: what still needs attention
    # is what the user is asking about, and burying it under last week's
    # finished jobs is the wrong way round.
    shown.sort(key=lambda j: (j["state"] not in jobs.OPEN, j["id"]))
    lines = [jobs.describe(j) for j in shown]

    waiting = [j for j in shown if j["state"] == jobs.WAITING_APPROVAL]
    if waiting:
        lines.append(
            f"\n{len(waiting)} job{'s' if len(waiting) > 1 else ''} "
            f"{'are' if len(waiting) > 1 else 'is'} blocked waiting for the user to approve something."
        )
    return "\n".join(lines)


@tool(
    name="job_status",
    description=(
        "The full history of one background job, by its id — every state it moved "
        "through and when. Use this when the user asks what happened to a specific "
        "job, why one failed, or why one is stuck."
    ),
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "The id of the job to look at."}},
        "required": ["id"],
    },
)
def job_status(id: int) -> str:
    job = jobs.require(id)

    lines = [jobs.describe(job), f"Created {when.stamp(job['created_at'])}."]
    if job.get("budget_usd") is not None:
        lines.append(f"Budget: {job['budget_usd']} USD.")
    if job.get("tools"):
        lines.append(f"Restricted to these tools: {', '.join(job['tools'])}.")

    lines.append("History:")
    for entry in job.get("log", []):
        lines.append(f"  {when.stamp(entry['at'])} — {entry['state']}: {entry['note']}")
    return "\n".join(lines)


@tool(
    name="cancel_job",
    description=(
        "Stop a background job for good, by its id. Use this when the user says to "
        "cancel, drop, or forget about something they queued earlier. A cancelled "
        "job never runs — if they might want it later, leave it queued instead."
    ),
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "The id of the job to cancel."}},
        "required": ["id"],
    },
)
def cancel_job(id: int) -> str:
    job = jobs.require(id)
    if job["state"] in jobs.TERMINAL:
        # Not an error worth raising: the user's intent — that this job doesn't
        # run — is already true. Say what actually happened instead.
        return f"Job #{id} was already {job['state']}; nothing to cancel."

    was = _OPEN_LABEL.get(job["state"], job["state"])
    jobs.cancel(id)
    return f"Cancelled job #{id} ({was}): {job['task']}"
