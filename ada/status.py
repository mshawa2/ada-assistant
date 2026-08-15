"""Shared, thread-safe 'what is Ada doing right now' state — read by the
face's dashboard, written by the REPLs and the agent core as a turn runs.
"""

import threading

_lock = threading.Lock()
_status = "idle"
_pending_confirmation: str | None = None


def set_status(s: str) -> None:
    global _status
    with _lock:
        _status = s


def get_status() -> str:
    with _lock:
        return _status


def set_pending_confirmation(message: str | None) -> None:
    global _pending_confirmation
    with _lock:
        _pending_confirmation = message


def get_pending_confirmation() -> str | None:
    with _lock:
        return _pending_confirmation
