"""Asking a human to approve something, from wherever they happen to be.

`AGENT.md` recorded why the browser could only *watch* a confirmation: the
terminal sits in a blocking `input()`, and a web click has no way into it. The
obvious fix — read stdin on a background thread — is worse than it looks. That
thread stays blocked after the browser answers, and then quietly eats the next
line you type as if it were the answer to a question already settled.

So stdin is *polled* rather than blocked on, with `select`. Each round checks
whether the browser got there first; if it did, the loop stops touching stdin
at all. Whoever answers first wins, nobody's next keystroke is stolen, and the
Tier 6 gate above this is completely unchanged — it still just calls a function
that returns True or False.

When stdin isn't a terminal (piped input, a test) `select` isn't meaningful, so
it falls back to a plain read. Still failing to a decline, as before, if
there's nobody there at all.
"""

import select
import sys

from ada import status

_POLL_SECONDS = 0.2

YES = ("y", "yes")


def ask(description: str, speak=None) -> bool:
    """Ask for approval. True only if someone actively says yes.

    `speak`, if given, is called with the question so voice front ends can say
    it aloud — the answer still comes from a keypress or a click, never from
    speech recognition. That was a deliberate Tier 6 decision: a misheard
    "yes" is not a risk worth taking on a safety gate.
    """
    pending = status.begin_confirmation(description)
    try:
        print(f"\n[confirm] {description}\n[y/N] (or answer in the browser) ", end="", flush=True)
        if speak is not None:
            try:
                speak(description)
            except Exception as e:
                print(f"\n[voice error: {e}]", flush=True)

        approved = _race_for_an_answer(pending)
        print("approved." if approved else "declined.", flush=True)
        return approved
    finally:
        status.end_confirmation()


def _race_for_an_answer(pending) -> bool:
    if not sys.stdin.isatty():
        # Piped or redirected: select reports "ready" forever at EOF, so just
        # read once and let the browser be irrelevant here.
        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            return False
        if not line:
            return False
        pending.answer(line.strip().lower() in YES)
        return bool(pending.approved)

    while not pending.answered.is_set():
        try:
            ready, _, _ = select.select([sys.stdin], [], [], _POLL_SECONDS)
        except (OSError, ValueError):
            # stdin went away underneath us — wait for the browser instead.
            return pending.wait()
        except KeyboardInterrupt:
            return False

        if not ready:
            continue

        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            return False
        if not line:  # EOF — nobody there to answer
            return False

        pending.answer(line.strip().lower() in YES)

    return bool(pending.approved)
