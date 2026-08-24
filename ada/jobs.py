"""Work that outlives a turn.

Everything Ada does today is one turn in one loop: you ask, tools run, the
reply lands, the turn is over. Nothing survives it. The heartbeat is the one
exception and it runs *checks* — plain Python functions on a fixed interval,
not work you can start, name, inspect, or stop.

A job is the missing primitive: a durable record of something to be done,
with a lifecycle. Start one, close the laptop, come back, ask what happened.

This lives outside `tools/` for the same reason `when.py` does — the
heartbeat's executor needs it as much as the model does, and two copies of a
state machine is two chances to drift.

## The state machine

    pending ──claim──> running ──finish──> done
       │                  │
       │                  ├──park──> waiting_approval ──approve──> pending
       │                  │                           ──deny────> cancelled
       │                  ├──fail──> failed ──retry──> pending
       │                  └──reap──> pending         (owner died)
       │
       └──cancel──> cancelled

An approved job goes back to `pending`, not straight to `running`. Parking
releases the worker — it has already moved on to something else — so there is
nobody to resume *into*. The job rejoins the queue and the next executor claims
it like any other, carrying a one-shot grant for the action that was approved.

`done` and `cancelled` are terminal and nothing moves them. An illegal
transition raises rather than silently doing nothing: a job that quietly
refuses to change state is a job you'll believe is running forever.

## Why claiming is a separate step

The heartbeat thread and the REPL thread both live in this process, and the
launchd daemon is a second process entirely. Two of them reading the same
pending job and both starting it is the exact failure `heartbeat/singleton.py`
exists to prevent one tier up. `claim()` does the read and the write under one
lock, so the loser sees `running` and moves on.

## Orphans

A process that dies mid-job leaves it stuck in `running` with nobody working
it. `reap_orphans()` checks whether the recorded owner is still alive and puts
abandoned work back on the queue, up to `MAX_ATTEMPTS` — after that it fails
loudly, because a job that crashes the process every time should stop being
retried rather than crash it forever.

## Seams for what comes next

Three fields are written now and read later, on purpose: adding them once the
file has real jobs in it means migrating records that already exist.

- `tools` — the allowlist a background turn may call. `routing.py` computes a
  relevant subset today as an *optimization, never a capability limit*, which
  is right for conversation and exactly wrong for unattended work. This is
  where least privilege gets enforced instead of suggested.
- `budget_usd` — this job's slice. `budget.per_day` is one global pool, so
  without a slice one runaway job starves the conversation you're having.
- `pending_approval` — the parked action from a confirmation nobody was there
  to answer. `confirm` blocks on a human at a terminal; at 3am there isn't
  one, and neither auto-approving `send_message` nor auto-denying it is a
  defensible answer. The job waits instead.
- `granted` — the *answer*, once a human gives one: a one-shot authorisation
  the next run consumes. It is matched on the tool name and the exact
  arguments, so approving "text Sam about the boiler" does not authorise
  texting Sam anything else. Different arguments ask again.

Nothing in this module calls a model. That's deliberate — the lifecycle is
worth having correct and tested before anything unattended runs inside it.
"""

import os
import time

from ada import store, when

_STORE = store.DATA_DIR / "jobs.json"
_lock = store.lock_for(_STORE)

# States.
PENDING = "pending"
RUNNING = "running"
WAITING_APPROVAL = "waiting_approval"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

STATES = (PENDING, RUNNING, WAITING_APPROVAL, DONE, FAILED, CANCELLED)
TERMINAL = (DONE, CANCELLED)
OPEN = (PENDING, RUNNING, WAITING_APPROVAL)

# What may follow what. A state missing from a set is a transition that raises.
_ALLOWED: dict[str, set[str]] = {
    PENDING: {RUNNING, CANCELLED},
    # Back to PENDING is the orphan case: the owner died, so the work returns
    # to the queue rather than being lost or left looking alive.
    RUNNING: {DONE, FAILED, WAITING_APPROVAL, CANCELLED, PENDING},
    # Approved work rejoins the queue rather than resuming in place: the
    # worker that parked it released ownership and moved on.
    WAITING_APPROVAL: {PENDING, CANCELLED, FAILED},
    DONE: set(),
    FAILED: {PENDING},
    CANCELLED: set(),
}

# Tries before a job is failed for good. Three is enough to ride out a flaky
# network and few enough that a job which kills the process stops taking the
# process down with it.
MAX_ATTEMPTS = 3


class JobError(Exception):
    """A job was asked to do something its current state doesn't allow."""


# --- storage ---------------------------------------------------------------


def _load() -> list[dict]:
    return store.read_json(_STORE, [])


def _save(jobs: list[dict]) -> None:
    store.write_json(_STORE, jobs)


def load_jobs() -> list[dict]:
    """Public, lock-safe read — used by the tools and by the executor."""
    with _lock:
        return _load()


def get(job_id: int) -> dict | None:
    """One job by id, or None."""
    with _lock:
        return next((j for j in _load() if j["id"] == job_id), None)


def require(job_id: int) -> dict:
    """One job by id, or a clear error the model can act on."""
    job = get(job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id}.")
    return job


# --- lifecycle -------------------------------------------------------------


def create(
    task: str,
    run_at: float | None = None,
    tools: list[str] | None = None,
    budget_usd: float | None = None,
) -> dict:
    """Put a new job on the queue.

    `run_at` is honoured by `claim()` from the start even though nothing
    schedules yet — a job store that can't express "not before 3pm" would have
    to grow the concept later, and by then there are records without it.
    """
    task = (task or "").strip()
    if not task:
        raise ValueError("A job needs a task — say what should actually be done.")

    with _lock:
        jobs = _load()
        job = {
            "id": max((j["id"] for j in jobs), default=0) + 1,
            "task": task,
            "state": PENDING,
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "attempts": 0,
            "owner_pid": None,
            "run_at": run_at,
            "tools": tools,
            "budget_usd": budget_usd,
            "pending_approval": None,
            "granted": None,
            "log": [],
        }
        _note(job, PENDING, "created")
        jobs.append(job)
        _save(jobs)
        return job


def _note(job: dict, state: str, note: str) -> None:
    """Append to a job's own history.

    Kept on the record rather than only in `audit.log` so "what happened to
    job 4?" is answerable from the job itself, which is what anyone actually
    asks. The audit log stays the tamper-evident account of tool calls; this
    is the job's story.
    """
    job.setdefault("log", []).append({"at": time.time(), "state": state, "note": note})


def _transition(job_id: int, to_state: str, note: str, **updates) -> dict:
    """Move one job to `to_state`, under the lock, or raise.

    Read-decide-write happens inside a single lock hold, so two callers racing
    the same job produce one winner and one clear error rather than two
    half-applied updates.
    """
    if to_state not in STATES:
        raise ValueError(f"'{to_state}' isn't a job state.")

    with _lock:
        jobs = _load()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            raise ValueError(f"No job with id {job_id}.")

        current = job["state"]
        if to_state not in _ALLOWED.get(current, set()):
            raise JobError(
                f"Job #{job_id} is {current}; it can't go to {to_state}. "
                f"From {current} it can only go to: "
                f"{', '.join(sorted(_ALLOWED.get(current, set()))) or 'nowhere — it has finished'}."
            )

        job["state"] = to_state
        job.update(updates)
        _note(job, to_state, note)
        _save(jobs)
        return job


def claim(now: float | None = None) -> dict | None:
    """Take the next eligible pending job, or None if there isn't one.

    The whole point of this function is that the read and the write are one
    atomic step. Anything that instead lists pending jobs and then starts one
    has a window in which a second worker does the same, and the job runs
    twice — which for a job whose tools include `send_message` means the user
    gets texted twice.
    """
    now = time.time() if now is None else now

    with _lock:
        jobs = _load()
        eligible = [
            j
            for j in jobs
            if j["state"] == PENDING and (j.get("run_at") is None or j["run_at"] <= now)
        ]
        if not eligible:
            return None

        # Whatever became due earliest, then by id, so a queue drains in the
        # order things were asked for rather than in dict order.
        eligible.sort(key=lambda j: (j.get("run_at") or j["created_at"], j["id"]))
        job = eligible[0]

        job["state"] = RUNNING
        job["started_at"] = now
        job["owner_pid"] = os.getpid()
        job["attempts"] = job.get("attempts", 0) + 1
        _note(job, RUNNING, f"claimed by pid {os.getpid()} (attempt {job['attempts']})")
        _save(jobs)
        return job


def finish(job_id: int, result: str) -> dict:
    """The job did what it was asked. `result` is what to tell the user."""
    return _transition(
        job_id,
        DONE,
        "finished",
        result=result,
        error=None,
        finished_at=time.time(),
        owner_pid=None,
        pending_approval=None,
        granted=None,
    )


def fail(job_id: int, error: str) -> dict:
    """The job couldn't be done. Stays inspectable; `retry` puts it back."""
    return _transition(
        job_id,
        FAILED,
        f"failed: {error}",
        error=error,
        finished_at=time.time(),
        owner_pid=None,
        pending_approval=None,
        granted=None,
    )


def park(job_id: int, approval: dict) -> dict:
    """Hold the job: it wants to do something that needs a human.

    `approval` is whatever the confirmation layer needs to render the question
    later and replay the action once answered — at minimum the tool name, its
    input, and the plain-language description `agent._describe_action` already
    produces. This module doesn't interpret it; step 2 owns that shape.
    """
    if not approval:
        raise ValueError("Parking a job needs the action that's waiting for approval.")
    return _transition(
        job_id,
        WAITING_APPROVAL,
        f"waiting on approval: {approval.get('description', approval.get('tool', 'an action'))}",
        pending_approval=approval,
        owner_pid=None,
    )


def resume(job_id: int) -> dict:
    """A human approved the parked action. Back on the queue, grant in hand.

    Not straight to `running`: parking released the worker, so there is no
    process still sitting in this job waiting to be told to carry on. It
    rejoins the queue and whichever executor claims it next finds `granted`
    waiting — the one action a human has already said yes to.
    """
    with _lock:
        job = require(job_id)
        return _transition(
            job_id,
            PENDING,
            "approved — requeued with a one-shot grant",
            granted=job.get("pending_approval"),
            pending_approval=None,
            started_at=None,
            owner_pid=None,
        )


def consume_grant(job_id: int, tool: str, tool_input: dict) -> bool:
    """Spend the one-shot approval, if it matches this exact call.

    Matched on the tool *and* its full arguments, not the tool alone. A grant
    for "text Sam about the boiler" must not authorise texting Sam anything
    else, and a model that comes back with different arguments after a human
    said yes is precisely the case where asking again is right.

    One shot: a matched grant is cleared before the tool runs, so a retry loop
    can't spend the same approval twice.
    """
    with _lock:
        jobs = _load()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            return False

        grant = job.get("granted")
        if not grant or grant.get("tool") != tool or grant.get("input") != tool_input:
            return False

        job["granted"] = None
        _note(job, job["state"], f"used the approval for {tool}")
        _save(jobs)
        return True


def cancel(job_id: int, reason: str = "cancelled by the user") -> dict:
    """Stop a job for good. Terminal — a cancelled job never runs again."""
    return _transition(
        job_id,
        CANCELLED,
        reason,
        finished_at=time.time(),
        owner_pid=None,
        pending_approval=None,
        granted=None,
    )


def retry(job_id: int) -> dict:
    """Put a failed job back on the queue, attempts intact."""
    return _transition(job_id, PENDING, "requeued for another attempt", error=None, finished_at=None)


# --- orphans ---------------------------------------------------------------


def _process_alive(pid: int | None) -> bool:
    """Whether `pid` is still running.

    Signal 0 checks for the process without touching it. A PermissionError
    means it exists and belongs to someone else — still alive, and the safe
    reading either way: treating a live process as dead is what causes the
    double-run this whole module is arranged to prevent.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_orphans() -> list[dict]:
    """Recover jobs whose owner died. Returns what changed.

    Called at startup, before the heartbeat begins its first pass: a laptop
    that closed mid-job should find the work waiting when it opens, not a
    record that claims to be running inside a process that no longer exists.

    A job over `MAX_ATTEMPTS` is failed rather than requeued. Something that
    takes the process down with it every time is not going to succeed on the
    fourth go, and an infinite retry loop on a job with real tools is worse
    than an unfinished job.
    """
    reaped = []
    with _lock:
        jobs = _load()
        for job in jobs:
            if job["state"] != RUNNING or _process_alive(job.get("owner_pid")):
                continue

            dead_pid = job.get("owner_pid")
            job["owner_pid"] = None
            if job.get("attempts", 0) >= MAX_ATTEMPTS:
                job["state"] = FAILED
                job["error"] = (
                    f"Gave up after {job['attempts']} attempts — the process running it "
                    "died each time without finishing."
                )
                job["finished_at"] = time.time()
                _note(job, FAILED, f"owner pid {dead_pid} died; out of attempts")
            else:
                job["state"] = PENDING
                job["started_at"] = None
                _note(job, PENDING, f"owner pid {dead_pid} died; requeued")
            reaped.append(job)

        if reaped:
            _save(jobs)
    return reaped


# --- display ---------------------------------------------------------------


def describe(job: dict) -> str:
    """One job as a line a person reads."""
    line = f"#{job['id']} [{job['state']}] {job['task']}"

    if job["state"] == PENDING and job.get("run_at"):
        line += f" — not before {when.stamp(job['run_at'])}"
    elif job["state"] == WAITING_APPROVAL:
        approval = job.get("pending_approval") or {}
        wants = approval.get("description") or approval.get("tool") or "an action"
        line += f" — waiting on you: {wants}"
    elif job["state"] == DONE and job.get("result"):
        line += f" — {job['result']}"
    elif job["state"] == FAILED and job.get("error"):
        line += f" — {job['error']}"

    if job.get("attempts", 0) > 1:
        line += f" (attempt {job['attempts']})"
    return line
