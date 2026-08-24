"""The check that actually runs a job.

Everything below this tier arranges for work to exist, wait, and be answered
for. This is the part that gives a job a model turn — and it's the first time
Ada does anything with tools while nobody is watching, which is why it lands
last and why every rail it needs was built before it.

## Where it runs

As a heartbeat check, so it inherits the things the heartbeat already
guarantees: `singleton.py` means exactly one process owns it, so two executors
can't race for the same job on top of the race `jobs.claim()` already prevents;
the kill switch pauses it with everything else; and a check that raises is
caught and logged rather than taking the loop down.

One job per tick by default. Checks run sequentially in one thread, so a job
that thinks for forty seconds is forty seconds a due reminder doesn't fire.
Draining a queue of ten in one tick would make that ten times worse.

## What the model is told

Not the conversational prompt. A job turn is framed as what it is — unattended,
with no user to ask and no follow-up coming — and told that its final reply *is*
the result the user will read. Without that it writes "let me know if you'd like
me to dig further", to nobody, and the job's stored result is a question.

## Three ways a turn ends

- **It finishes.** The last assistant text becomes the job's result, and a
  notice carries it to the user.
- **It parks** (`approvals.Parked`). Not a failure — the job is already
  `waiting_approval`, and the notice went out when it parked. The turn's
  messages are **discarded rather than persisted**: the last `tool_use` block
  never got its `tool_result`, and the API rejects a dangling tool call on the
  way back in.
- **It breaks.** Anything else fails the job with the error kept for
  inspection, including `LLMError` from being over the global budget — a job is
  not a good reason to blow through a spending cap.

## What isn't here yet

A job re-run after approval **starts its turn over**. Whatever it did before
parking, it does again. That's survivable because the gated actions — the ones
that can't be taken twice — are exactly the ones that stop and ask, and a grant
is matched to exact arguments. It is still the strongest argument for
persisting a job's transcript, which is the next tier's job.
"""

from ada import approvals, audit, cost, jobs
from ada.heartbeat.notices import add_notice

# What the user sees of a result before it's cut short. A notice is a nudge to
# come and look, not the place to paste an essay — `job_status` has the whole
# thing.
RESULT_PREVIEW = 400


def _prompt(job: dict) -> str:
    """How a job asks the model for what it wants.

    Deliberately not phrased as the user speaking. The model is told plainly
    that nobody is there, so it stops offering to do more and just does it.
    """
    return (
        f"[Background job #{job['id']}. You are running unattended — the user is "
        "not here, there is no conversation, and nothing you ask will be "
        "answered.]\n\n"
        f"Task: {job['task']}\n\n"
        "Do this now with the tools you have. Your final reply is the whole "
        "result the user will be shown, so make it complete and readable on its "
        "own — they won't see this working-out. Don't offer to do more, don't "
        "ask questions, and don't promise to follow up.\n\n"
        "If something you do needs the user's approval, just call the tool: it "
        "will stop and ask them, and the job carries on once they answer. If you "
        "genuinely can't finish, say plainly what you got and what stopped you."
    )


def _final_text(messages: list[dict]) -> str:
    """The model's last actual words, which are the job's result.

    Read back off the message list rather than accumulated from the text
    stream, because the stream carries every round's commentary — "let me check
    that" is not a result.
    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        parts = []
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if kind != "text":
                continue
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
            if text:
                parts.append(text)
        joined = "".join(parts).strip()
        if joined:
            return joined
    return ""


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= RESULT_PREVIEW else text[:RESULT_PREVIEW].rstrip() + "…"


def run_one(job: dict, level: str = "urgent") -> str:
    """Give one claimed job a turn. Returns the state it ended in."""
    # Imported here rather than at module scope: `ada.agent` pulls in the whole
    # tool registry, and the heartbeat has to be importable without it.
    from ada import agent

    job_id = job["id"]

    if jobs.over_budget(job):
        spent, cap = job.get("spent_usd", 0.0), job.get("budget_usd")
        jobs.fail(job_id, f"out of budget — this job has spent {spent:.4f} of its {cap} limit")
        add_notice("jobs", f"Job #{job_id} stopped: it used up its budget.", level=level)
        return jobs.FAILED

    messages = [{"role": "user", "content": _prompt(job)}]
    audit.log("job_started", job=job_id, task=job["task"], tools=job.get("tools"))

    with cost.meter() as spent_so_far:
        try:
            agent.run_turn(
                messages,
                on_text=lambda _: None,
                on_status=lambda _: None,
                confirm=approvals.headless(job_id),
                allowed_tools=job.get("tools"),
            )
        except approvals.Parked:
            # Already `waiting_approval`, and the notice went out when it
            # parked. `messages` is deliberately dropped on the floor.
            jobs.record_spend(job_id, spent_so_far())
            audit.log("job_parked", job=job_id)
            return jobs.WAITING_APPROVAL
        except Exception as e:
            # Includes LLMError, which is how being over the global spending
            # cap arrives here. A background job is not a good enough reason to
            # blow through a limit the user set.
            jobs.record_spend(job_id, spent_so_far())
            jobs.fail(job_id, str(e) or e.__class__.__name__)
            audit.log("job_failed", job=job_id, error=str(e))
            add_notice("jobs", f"Job #{job_id} failed: {job['task']} — {e}", level=level)
            return jobs.FAILED

        jobs.record_spend(job_id, spent_so_far())

    result = _final_text(messages) or "Finished, but didn't say anything about it."
    jobs.finish(job_id, result)
    audit.log("job_finished", job=job_id)
    add_notice("jobs", f"Job #{job_id} done — {job['task']}: {_preview(result)}", level=level)
    return jobs.DONE


def run_jobs(params: dict) -> None:
    """The heartbeat check. Claims what's due and runs it."""
    limit = max(1, int(params.get("max_jobs_per_tick", 1)))
    level = params.get("level", "urgent")

    for _ in range(limit):
        job = jobs.claim()
        if job is None:
            return
        run_one(job, level=level)
