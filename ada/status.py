"""What Ada is doing right now — shared, thread-safe, and readable by a face.

This started as a single status string for a read-only dashboard. It now
carries enough for that dashboard to be an actual interface: the live
conversation, which tool is running, and confirmations that can be answered
from either the terminal or the browser.

**Deliberately in memory, not on disk.** The durable transcript lives in
`data/session.json`; this is the last few turns, for showing. Two stores with
different jobs beats one store doing both badly — and a display buffer has no
business surviving a restart.

**Confirmations are a race, on purpose.** `AGENT.md` recorded why the browser
could only *watch* a confirmation: the terminal sits in a blocking `input()`,
and a web click can't reach into it. The fix isn't to move the prompt — it's to
let both answer. A `Pending` carries an Event; whoever gets there first sets
it, the other is ignored, and the gate itself is unchanged. Declining is still
what happens if nobody answers at all.
"""

import itertools
import threading
import time

MAX_TRANSCRIPT = 40

# Coarse states a face can animate against, derived from the status strings
# the REPLs already emit. Deriving them here means the front end never has to
# pattern-match on prose.
IDLE = "idle"
LISTENING = "listening"
TRANSCRIBING = "transcribing"
THINKING = "thinking"
TOOL = "tool"
SPEAKING = "speaking"
WAITING = "waiting"

_lock = threading.RLock()
_status = "idle"
_transcript: list[dict] = []
_current_tool: str | None = None
_pending = None
_ids = itertools.count(1)


class Pending:
    """A confirmation waiting on a human, from wherever one turns up."""

    def __init__(self, description: str):
        self.id = next(_ids)
        self.description = description
        self.answered = threading.Event()
        self.approved = None

    def answer(self, approved: bool) -> bool:
        """Record an answer. False if someone already got there first."""
        with _lock:
            if self.answered.is_set():
                return False
            self.approved = bool(approved)
            self.answered.set()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        """Block until answered. Declines if nobody ever does."""
        self.answered.wait(timeout)
        return bool(self.approved)


def _kind(status: str) -> str:
    if _pending is not None:
        return WAITING
    lowered = (status or "").lower()
    if lowered.startswith("listening"):
        return LISTENING
    if lowered.startswith("transcribing"):
        return TRANSCRIBING
    if lowered.startswith("thinking"):
        return THINKING
    if lowered.startswith("using "):
        return TOOL
    if lowered.startswith("speaking"):
        return SPEAKING
    return IDLE


# --- status -----------------------------------------------------------------


def set_status(s: str) -> None:
    global _status, _current_tool
    with _lock:
        _status = s
        _current_tool = s[len("using ") :] if (s or "").startswith("using ") else None


def get_status() -> str:
    with _lock:
        return _status


def current_tool():
    with _lock:
        return _current_tool


# --- the live conversation --------------------------------------------------


def say(role: str, text: str) -> None:
    """Record a completed turn for display."""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        _transcript.append({"role": role, "text": text, "at": time.time(), "streaming": False})
        del _transcript[:-MAX_TRANSCRIPT]


def begin_reply() -> None:
    """Open an assistant turn that `stream` fills in as it generates."""
    with _lock:
        _transcript.append({"role": "ada", "text": "", "at": time.time(), "streaming": True})
        del _transcript[:-MAX_TRANSCRIPT]


def stream(chunk: str) -> None:
    """Append to the open reply, so the face shows words as they arrive."""
    if not chunk:
        return
    with _lock:
        if _transcript and _transcript[-1].get("streaming"):
            _transcript[-1]["text"] += chunk
        else:
            _transcript.append({"role": "ada", "text": chunk, "at": time.time(), "streaming": True})


def end_reply() -> None:
    with _lock:
        if _transcript and _transcript[-1].get("streaming"):
            _transcript[-1]["streaming"] = False
            if not _transcript[-1]["text"].strip():
                _transcript.pop()


def transcript() -> list[dict]:
    with _lock:
        return list(_transcript)


def clear_transcript() -> None:
    with _lock:
        _transcript.clear()


# --- confirmations ----------------------------------------------------------


def begin_confirmation(description: str) -> Pending:
    global _pending
    with _lock:
        _pending = Pending(description)
        return _pending


def end_confirmation() -> None:
    global _pending
    with _lock:
        _pending = None


def pending():
    with _lock:
        return _pending


def answer_pending(confirmation_id: int, approved: bool) -> bool:
    """Answer from outside the terminal — the browser's way in."""
    with _lock:
        current = _pending
    if current is None or current.id != confirmation_id:
        return False
    return current.answer(approved)


# Kept for the original dashboard's shape; the description is what it showed.
def set_pending_confirmation(message) -> None:
    if message is None:
        end_confirmation()
    else:
        begin_confirmation(message)


def get_pending_confirmation():
    current = pending()
    return current.description if current else None


# --- everything a face needs, in one read -----------------------------------


def snapshot() -> dict:
    with _lock:
        current = _pending
        return {
            "status": _status,
            "state": _kind(_status),
            "tool": _current_tool,
            "transcript": list(_transcript),
            "pending": ({"id": current.id, "description": current.description} if current else None),
        }
