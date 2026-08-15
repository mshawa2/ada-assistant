"""Exactly one process runs the heartbeat at a time.

Once there's a launchd agent beating in the background, opening a REPL would
mean two loops running the same checks against the same state files. The
per-check `*_notified` flags stop most duplicate notices, but "mostly" isn't a
guarantee — two processes can read the same unflagged reminder before either
writes, and you get told twice.

So the loop takes an exclusive lock first. Whoever holds it does the checking;
anyone else still reads notices, still shows the greeting, still answers you —
they just don't run checks. That makes "start the daemon, then open a REPL
whenever you like" safe by construction rather than by luck.

`fcntl.flock` is POSIX, which covers macOS and Linux. The lock is released by
the OS if the process dies, so a crash can't leave the heartbeat permanently
disabled.
"""

import fcntl
import os

from ada import store

_LOCK_FILE = store.DATA_DIR / "heartbeat.lock"
_handle = None


def acquire() -> bool:
    """Try to become the heartbeat owner. True if this process now owns it.

    Idempotent: a process that already holds the lock gets True rather than
    contending with itself, since flock treats two file descriptors onto the
    same file as independent even within one process.
    """
    global _handle
    if _handle is not None:
        return True

    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = _LOCK_FILE.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _handle = handle
    return True


def release() -> None:
    """Give up ownership. The OS does this anyway on exit — this is for tests
    and for a clean shutdown."""
    global _handle
    if _handle is None:
        return
    try:
        fcntl.flock(_handle, fcntl.LOCK_UN)
    finally:
        _handle.close()
        _handle = None


def owner_pid() -> int | None:
    """The PID currently running the heartbeat, or None if nobody is."""
    if not _LOCK_FILE.exists():
        return None
    try:
        return int(_LOCK_FILE.read_text().strip())
    except ValueError:
        return None
