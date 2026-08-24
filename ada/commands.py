"""Things Ada does without thinking about them.

Three front ends grew three different ideas of what a command is. The text
REPL knew `exit` and `reset`, inline in its loop; the two voice front ends knew
nothing, so saying "reset" out loud sent the word "reset" to the model, which
apologised and asked what you'd like to reset. That's the same drift
`ada/agent.py` exists to prevent one layer up — one tool-calling loop, many
ways in — and this is that rule applied to the other half of the input.

## What belongs here, and what doesn't

A command is not a cheaper tool. It's for the things a model turn **can't** do
or **shouldn't** be asked to:

- `exit` ends the process. A tool can't; it runs *inside* the turn.
- `reset` clears the transcript the model is reasoning from. Asking a model to
  forget the conversation it's mid-way through is a strange thing to ask.
- `status`, `pause`, `resume` and `help` are instant, free and exact. There are
  tools that overlap them, and the tools stay — but "pause" said out loud
  should stop the heartbeat *now*, not after a round trip that costs money and
  might come back with a paraphrase.

Everything else stays a tool. A command that could have been a tool is a
capability the model can no longer reason about or combine.

## Matching is whole-utterance, deliberately

"Let's reset the schedule for Monday" must not wipe the conversation, and in a
room where Ada is listening on an open mic the cost of a false positive is
much higher than the cost of a miss. So a command matches only when the whole
utterance *is* the command, after normalising case, punctuation and spacing —
never as a substring, and never as an intent to be inferred.

The phrasings are listed rather than guessed at, because speech comes back
through transcription: someone says "start over", not "reset". A miss is
harmless — it goes to the model, which will do the right thing anyway.
"""

from dataclasses import dataclass
from typing import Callable

from ada import approvals, budget, cost, jobs, session
from ada import status as ada_status
from ada.heartbeat.kill_switch import is_paused, set_paused

_PUNCTUATION = ".,!?;:'\"…-–—"


@dataclass(frozen=True)
class Result:
    """What a command did, and what the caller still has to do about it.

    `quit` and `reset` are handed back rather than done here because the caller
    owns those: the loop it's sitting in, and the live message list it hands
    the model. This module can clear what's on disk; only the front end can
    clear what's in its own hands.
    """

    text: str
    quit: bool = False
    reset: bool = False


@dataclass(frozen=True)
class Command:
    name: str
    phrases: tuple[str, ...]
    summary: str
    run: Callable[[], Result]


def normalize(utterance: str) -> str:
    """An utterance reduced to the words, for comparison.

    Speech arrives punctuated and capitalised by the transcriber — "Reset." and
    "reset" are the same instruction, and a command layer that can only see the
    second one works when typed and not when spoken.
    """
    words = (utterance or "").strip().lower().strip(_PUNCTUATION).split()
    return " ".join(word.strip(_PUNCTUATION) for word in words).strip()


# --- what the commands do ---------------------------------------------------


def _quit() -> Result:
    return Result("Bye!", quit=True)


def _reset() -> Result:
    session.clear()
    ada_status.clear_transcript()
    return Result(
        "Fresh conversation — what I remember about you is untouched.",
        reset=True,
    )


def _pause() -> Result:
    set_paused(True)
    return Result("Background checks paused. I'll keep answering, I just won't go looking.")


def _resume() -> Result:
    set_paused(False)
    return Result("Background checks running again — anything that came due while paused runs now.")


def _status() -> Result:
    """Ada's own state, without spending anything to find it out."""
    parts = ["Background checks are paused." if is_paused() else "Background checks are running."]

    open_jobs = [j for j in jobs.load_jobs() if j["state"] in jobs.OPEN]
    if open_jobs:
        plural = "s" if len(open_jobs) != 1 else ""
        parts.append(f"{len(open_jobs)} job{plural} open.")

    waiting = approvals.pending()
    if waiting:
        plural = "s" if len(waiting) != 1 else ""
        parts.append(f"{len(waiting)} job{plural} waiting on your approval.")

    if not open_jobs and not waiting:
        parts.append("Nothing queued.")

    spent = budget.spent(budget.MODEL, budget.WINDOWS["per_day"])
    parts.append(f"{budget.money(spent)} spent today.")
    return Result(" ".join(parts))


def _help() -> Result:
    lines = ["Things I'll do straight away, typed or spoken:"]
    for command in COMMANDS:
        lines.append(f"  {command.phrases[0]} — {command.summary}")
    lines.append("Anything else is just a conversation.")
    return Result("\n".join(lines))


COMMANDS: tuple[Command, ...] = (
    Command(
        "help",
        ("help", "commands", "what commands", "what can i say"),
        "this list",
        _help,
    ),
    Command(
        "status",
        ("status", "how are you doing", "what's going on", "whats going on", "what are you up to"),
        "what you're working on and what today has cost",
        _status,
    ),
    Command(
        "reset",
        ("reset", "start over", "start again", "start fresh", "new conversation", "forget this conversation"),
        "start a fresh conversation (durable memory untouched)",
        _reset,
    ),
    Command(
        "pause",
        ("pause", "be quiet", "go quiet", "stop checking"),
        "stop background checks",
        _pause,
    ),
    Command(
        "resume",
        ("resume", "unpause", "carry on", "start checking"),
        "start background checks again",
        _resume,
    ),
    Command(
        "exit",
        ("exit", "quit", "goodbye", "bye", "good night", "goodnight", "stop listening", "that's all", "thats all"),
        "shut down",
        _quit,
    ),
)

_BY_PHRASE = {phrase: command for command in COMMANDS for phrase in command.phrases}


def handle(utterance: str) -> Result | None:
    """Run `utterance` as a command, or None if it isn't one.

    None is the ordinary answer. Anything that isn't exactly a command is a
    thing to say to the model, and that has to stay the default — a command
    layer that guesses is a command layer that eats real requests.
    """
    command = _BY_PHRASE.get(normalize(utterance))
    return command.run() if command else None
