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

## Resuming

A job that stops — parked for approval, or waiting out a backoff — keeps its
transcript, so its next run picks up where it left off instead of redoing every
search that led there. What's stripped first is a trailing `tool_use` that
never got its `tool_result`: the API rejects a dangling tool call, and that
block is precisely the action that was interrupted.

So the model resumes seeing everything it did and re-decides the last step. If
it asks for the same thing again, the grant matches and it goes through. If it
asks for something else, that parks in its turn, which is the right answer
rather than a bug. **No note is injected saying "you were approved"** — the
transcript ends on a user `tool_result`, and appending another user message
there would put two user turns back to back to tell the model something it
doesn't need to know.

The transcript is dropped the moment a job reaches a terminal state. It's a
resume point, not a record; `result` and `log` are the record.
"""

from ada import approvals, audit, budget, cost, jobs, session
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


def _resumable(messages: list[dict], tool_results: list[dict] | None = None) -> list[dict]:
    """The transcript, made legal to send back.

    A turn that stops inside `_execute_tool_calls` has already appended the
    assistant message asking for its tools, and never appended their results.

    If the gate handed back the results it had — everything that ran before it
    stopped, plus an honest "paused, awaiting approval" for the one that
    didn't — the round is completed with those and kept whole. That matters
    more than it sounds: a model routinely asks for several tools at once, so
    dropping the round throws away work that actually happened.

    Failing that, the unanswered ask comes off the end and the model decides
    that step again from the last complete round.
    """
    plain = session.to_plain(messages)

    if tool_results and plain and plain[-1].get("role") == "assistant":
        return plain + [{"role": "user", "content": list(tool_results)}]
    while plain and plain[-1].get("role") == "assistant":
        content = plain[-1].get("content")
        if isinstance(content, list) and any(
            block.get("type") == "tool_use" for block in content if isinstance(block, dict)
        ):
            plain.pop()
            continue
        break
    return plain


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= RESULT_PREVIEW else text[:RESULT_PREVIEW].rstrip() + "…"


def _worth_retrying(error: Exception) -> bool:
    """Whether trying again could plausibly go differently.

    A rate limit or a dropped connection will. A breached spending cap won't —
    it will fail identically until the user raises the limit or the window
    rolls over, and burning the job's attempts against it just means the real
    error is three retries stale by the time anyone reads it.

    Checked on the cause rather than the message: `llm.py` wraps
    `BudgetExceeded` in an `LLMError`, and matching on wording is how a
    reworded error silently becomes retryable.
    """
    return not isinstance(getattr(error, "__cause__", None), budget.BudgetExceeded)


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

    resuming = job.get("transcript")
    messages = list(resuming) if resuming else [{"role": "user", "content": _prompt(job)}]
    audit.log(
        "job_started", job=job_id, task=job["task"], tools=job.get("tools"), resumed=bool(resuming)
    )

    with cost.meter() as spent_so_far:
        try:
            agent.run_turn(
                messages,
                on_text=lambda _: None,
                on_status=lambda _: None,
                confirm=approvals.headless(job_id),
                allowed_tools=job.get("tools"),
            )
        except approvals.Parked as parked:
            # Already `waiting_approval`, and the notice went out when it
            # parked. What's kept is where it got to — including any tools that
            # ran in the same batch before the gate stopped it, which the
            # exception carries out for exactly this reason.
            jobs.record_spend(job_id, spent_so_far())
            jobs.save_transcript(job_id, _resumable(messages, getattr(parked, "tool_results", None)))
            audit.log("job_parked", job=job_id)
            return jobs.WAITING_APPROVAL
        except Exception as e:
            jobs.record_spend(job_id, spent_so_far())
            reason = str(e) or e.__class__.__name__
            attempts = job.get("attempts", 1)

            if not _worth_retrying(e) or attempts >= jobs.MAX_ATTEMPTS:
                jobs.fail(job_id, reason)
                audit.log("job_failed", job=job_id, error=reason)
                add_notice("jobs", f"Job #{job_id} failed: {job['task']} — {reason}", level=level)
                return jobs.FAILED

            delay = jobs.backoff_for(attempts)
            # Kept for the same reason as parking: a job that broke on its
            # fourth search shouldn't redo the first three.
            jobs.save_transcript(job_id, _resumable(messages))
            jobs.retry_later(job_id, delay, reason)
            # Deliberately no notice: a transient failure that fixes itself on
            # the next attempt isn't news, and a notice per retry would train
            # the user to ignore the log. The final failure still speaks up.
            audit.log("job_retrying", job=job_id, error=reason, in_seconds=delay)
            return jobs.PENDING

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
