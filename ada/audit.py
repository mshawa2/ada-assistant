"""A plain, visible log of what Ada did and why — which tools ran, what the
heartbeat surfaced, what it asked to confirm. One JSON object per line, so
it's both greppable and structured.

This is the record you'd actually go read after something surprising, which
is exactly why it can't grow without bound: an unrotated log eventually makes
the dashboard's tail slow and fills the disk on the always-on host. Rotation
keeps the recent past cheap to read while still leaving several files of
history to grep through.
"""

import json
import time

from ada import store

_LOG_FILE = store.DATA_DIR / "audit.log"
_lock = store.lock_for(_LOG_FILE)

MAX_BYTES = 5 * 1024 * 1024
KEEP = 3


def _rotate_if_needed() -> None:
    """Roll audit.log → audit.log.1 → … → audit.log.KEEP, dropping the oldest.

    Caller must hold `_lock`. This runs *before* a write rather than after, so
    MAX_BYTES is a real ceiling instead of a line the file drifts past.
    """
    if not _LOG_FILE.exists() or _LOG_FILE.stat().st_size < MAX_BYTES:
        return

    _LOG_FILE.with_name(f"{_LOG_FILE.name}.{KEEP}").unlink(missing_ok=True)
    for i in range(KEEP - 1, 0, -1):
        source = _LOG_FILE.with_name(f"{_LOG_FILE.name}.{i}")
        if source.exists():
            source.replace(_LOG_FILE.with_name(f"{_LOG_FILE.name}.{i + 1}"))
    _LOG_FILE.replace(_LOG_FILE.with_name(f"{_LOG_FILE.name}.1"))


def log(event: str, **fields) -> None:
    # default=str so an odd value in a tool's input can never take down the
    # very thing you'd use to find out what that tool did.
    line = json.dumps({"time": time.time(), "event": event, **fields}, default=str) + "\n"
    with _lock:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        with _LOG_FILE.open("a") as f:
            f.write(line)


def tail(limit: int = 30) -> list[dict]:
    """The most recent `limit` entries, newest first.

    Unparseable lines are skipped rather than raised on: the log is appended
    to live, so a reader can catch a final partial line, and a dashboard
    refresh isn't worth a 500.
    """
    with _lock:
        if not _LOG_FILE.exists():
            return []
        lines = _LOG_FILE.read_text().splitlines()[-limit:]

    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries
