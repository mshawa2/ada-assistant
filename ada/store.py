"""Durable reads and writes for everything under `data/`.

Every store stays a plain file the user can open and hand-edit — that's
deliberate and it doesn't change here. What changes is *how* a write lands.
`Path.write_text()` truncates the real file before the new bytes go in, so a
crash, a Ctrl-C, or a full disk mid-write leaves a half-written file and the
store is gone. Writing to a sibling temp file and `os.replace()`-ing it into
place is atomic: a reader sees either the whole old file or the whole new
one, never a torn one.

Locks are keyed by resolved path and shared process-wide, so two modules
touching the same file — or the heartbeat thread racing the REPL thread —
serialize against each other instead of each holding a private lock that the
other knows nothing about.

`ADA_DATA_DIR` relocates the whole data directory. Tests use it to stay away
from real reminders and memory, and it's the seam that lets the always-on
host in AGENT.md keep its data somewhere other than next to the code.
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

DATA_DIR = Path(
    os.environ.get("ADA_DATA_DIR") or Path(__file__).resolve().parent.parent / "data"
)

_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    """The one lock guarding `path`, shared by every caller in this process.

    Reentrant, so a function that already holds it can call another that
    takes it too without deadlocking itself.
    """
    key = Path(path).resolve()
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def read_json(path: Path, default: Any) -> Any:
    """Read `path`, or return `default` if it isn't there yet.

    A file that exists but doesn't parse is a real problem, not something to
    paper over silently: it gets renamed aside so the bad bytes are still
    recoverable by hand, a loud line goes to the console, and the caller gets
    the default so Ada keeps running instead of dying on every model call.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        quarantine = path.with_name(f"{path.name}.corrupt")
        try:
            os.replace(path, quarantine)
        except OSError:
            quarantine = None
        moved = f"Moved it to {quarantine.name} and started fresh." if quarantine else "Left it in place."
        print(f"\n[ada: {path.name} was unreadable ({e}). {moved}]", flush=True)
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomically replace `path` with `data` as indented JSON."""
    write_text(path, json.dumps(data, indent=2) + "\n")


def write_text(path: Path, text: str) -> None:
    """Atomically replace `path` with `text`.

    The temp file is created in the destination's own directory on purpose —
    `os.replace()` is only atomic within a single filesystem, so a temp file
    in /tmp would silently degrade to a copy that can still tear.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
