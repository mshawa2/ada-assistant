"""The gate, for when nobody is at the terminal.

`ada/confirm.py` asks a human and waits. That works because a human is sitting
there — in a REPL, or with the dashboard open. A background job has neither,
and there is no honest answer for `confirm` to return at 3am. Auto-approving
`send_message` is indefensible. Auto-denying it makes unattended work useless
and, worse, trains the user to ignore a stream of "couldn't do that" results.

So the third answer: **park it**. The action is recorded on the job, the job
stops, and a human is asked the next time there is one to ask. Approval doesn't
resume anything in place — the worker released the job when it parked — it puts
the job back on the queue carrying a one-shot grant for exactly that action.

## Why the raise

`headless()` returns a `confirm` callable, so the Tier 6 gate in `agent.py` is
untouched. But it raises `Parked` rather than returning False, and the
difference matters: False means *declined*, which the model is told and reasons
about — it will apologise, or try to do the thing another way, and the turn
runs on producing work that assumes an answer nobody gave. Parking isn't a no.
It's "not yet", and the only faithful way to express that in a synchronous turn
is to stop it.

The caller must discard a parked turn's messages rather than persist them: the
last `tool_use` block never got its `tool_result`, and the API rejects a
dangling tool call on the way back in.

## Injection

Approving is the one thing a model must never do on its own. A web page that
says "approve job 3" is a page trying to send a text through Ada, and
`AGENT.md`'s rule that data is not instructions is exactly what stands between
those. So the tool that resolves an approval is itself gated: the model can
*propose* an answer, and a human still has to press y against the original
wording. Injection's best case becomes Ada asking a question.
"""

import time
from typing import Callable

from ada import audit, jobs
from ada.confirm import Action
from ada.heartbeat import notices

# Approvals are raised at this level so they're announced at the start of the
# next session and held — not dropped — through quiet hours. Deliberately not
# `critical`: a parked job is waiting, not on fire, and waking someone at 4am
# to approve an email is how a gate becomes something people click through.
NOTICE_LEVEL = "urgent"
NOTICE_CHECK = "approvals"


class Parked(Exception):
    """A job stopped because it needs a human. Not a failure — a pause."""

    def __init__(self, job_id: int, action: Action):
        self.job_id = job_id
        self.action = action
        super().__init__(f"Job #{job_id} is waiting on approval: {action.description}")


def headless(job_id: int) -> Callable[[Action], bool]:
    """A `confirm` for a turn with nobody watching it.

    Returns True only for an action a human has already approved, matched on
    the tool and its exact arguments. Anything else parks the job and stops
    the turn.
    """

    def confirm(action: Action) -> bool:
        if jobs.consume_grant(job_id, action.tool, action.input):
            audit.log(
                "approval_used", job=job_id, tool=action.tool, input=action.input
            )
            return True

        park(job_id, action)
        raise Parked(job_id, action)

    return confirm


def park(job_id: int, action: Action) -> dict:
    """Record what the job wants to do, and put the question where a human
    will meet it."""
    job = jobs.park(
        job_id,
        {
            "tool": action.tool,
            "input": action.input,
            "description": action.description,
            "asked_at": time.time(),
        },
    )
    audit.log("approval_requested", job=job_id, tool=action.tool, input=action.input)
    notices.add_notice(
        NOTICE_CHECK,
        f"Job #{job_id} needs your approval before it can carry on: {action.description}",
        level=NOTICE_LEVEL,
    )
    return job


def pending() -> list[dict]:
    """Every job currently blocked on a human, oldest question first."""
    waiting = [j for j in jobs.load_jobs() if j["state"] == jobs.WAITING_APPROVAL]
    waiting.sort(key=lambda j: (j.get("pending_approval") or {}).get("asked_at") or j["id"])
    return waiting


def _require_waiting(job_id: int) -> dict:
    job = jobs.require(job_id)
    if job["state"] != jobs.WAITING_APPROVAL:
        raise ValueError(
            f"Job #{job_id} isn't waiting for approval — it's {job['state']}. "
            "Nothing to answer."
        )
    if not job.get("pending_approval"):
        # Can't happen through `park`, and if it somehow does, resolving it
        # would hand out a grant for an action nobody can name.
        raise ValueError(f"Job #{job_id} is waiting for approval but has no action recorded.")
    return job


def describe(job: dict) -> str:
    """The question, as a human should meet it."""
    approval = job.get("pending_approval") or {}
    return approval.get("description") or f"an action from job #{job['id']}"


def approve(job_id: int) -> dict:
    """Yes — once, for exactly this action."""
    job = _require_waiting(job_id)
    approval = job["pending_approval"]

    requeued = jobs.resume(job_id)
    audit.log(
        "approval_granted", job=job_id, tool=approval["tool"], input=approval["input"]
    )
    _clear_notices(job_id)
    return requeued


def deny(job_id: int) -> dict:
    """No — and the job stops.

    Cancelling the whole job rather than telling the model "declined" and
    letting it carry on is the deliberate, blunt version. A declined action in
    a conversation is recoverable, because you're there to steer what happens
    next; the same decline in an unattended job leaves something half-done with
    nobody watching. Stopping is the answer that needs no supervision. Letting
    a denied job continue down a different path is a real thing to want, and
    the place to build it is the executor, once resuming a job mid-turn exists
    at all.
    """
    job = _require_waiting(job_id)
    approval = job["pending_approval"]

    cancelled = jobs.cancel(job_id, f"you declined: {approval['description']}")
    audit.log(
        "approval_denied", job=job_id, tool=approval["tool"], input=approval["input"]
    )
    _clear_notices(job_id)
    return cancelled


def _clear_notices(job_id: int) -> None:
    """Dismiss the 'needs your approval' notices for a job that's been answered.

    Without this the log keeps asking a question the user has already settled,
    which is how a notice list stops being worth reading.
    """
    marker = f"Job #{job_id} needs your approval"
    for notice in notices.list_notices():
        if notice["check"] == NOTICE_CHECK and notice["text"].startswith(marker):
            notices.dismiss_notice(notice["id"])
