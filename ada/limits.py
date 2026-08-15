"""How often a tool may run, regardless of anything else.

`confirm` asks you before each action, which is the right protection against
*one* wrong action. It is not a protection against a hundred of them: a model
in a retry loop will happily present the same prompt over and over, and the
human in the loop is exactly the part that gets tired and starts saying yes.

So consequential tools also get a ceiling. Sending someone thirty texts is a
real harm even if you technically approved each one, and it's the kind of harm
that's obvious in hindsight and invisible at 11pm.

Checked *before* the confirmation prompt on purpose. Being over the limit
should stop the prompt appearing at all, not give you one more thing to click
through.

Usage is recorded only after a tool actually runs, so a declined action never
counts against you.
"""

import time

from ada import settings, store

_STORE = store.DATA_DIR / "tool_usage.json"
_lock = store.lock_for(_STORE)

WINDOWS = {"per_minute": 60, "per_hour": 3600, "per_day": 86400}
_LABELS = {"per_minute": "minute", "per_hour": "hour", "per_day": "day"}


class LimitExceeded(Exception):
    """Raised when a tool has hit its configured ceiling."""


def _limits_for(tool_name: str) -> dict:
    limits = settings.get("autonomy.limits", {}) or {}
    return limits.get(tool_name, {}) or {}


def _load() -> dict:
    return store.read_json(_STORE, {})


def check(tool_name: str) -> None:
    """Raise LimitExceeded if `tool_name` has hit a configured ceiling."""
    limits = _limits_for(tool_name)
    if not limits:
        return

    now = time.time()
    with _lock:
        history = _load().get(tool_name, [])

    for window_name, seconds in WINDOWS.items():
        ceiling = limits.get(window_name)
        if ceiling is None:
            continue
        used = sum(1 for stamp in history if now - stamp < seconds)
        if used >= ceiling:
            label = _LABELS[window_name]
            raise LimitExceeded(
                f"'{tool_name}' has already run {used} times in the last {label} — its limit "
                f"is {ceiling} per {label}. Tell the user plainly and don't retry; they can "
                "raise it in their settings."
            )


def record(tool_name: str) -> None:
    """Note that `tool_name` just ran. No-op for tools with no limit set."""
    limits = _limits_for(tool_name)
    windows = [WINDOWS[name] for name in limits if name in WINDOWS]
    if not windows:
        return

    now = time.time()
    longest = max(windows)

    with _lock:
        usage = _load()
        # Pruned to the longest window we could still be asked about, so this
        # file can't grow without bound the way an append-only log would.
        history = [stamp for stamp in usage.get(tool_name, []) if now - stamp < longest]
        history.append(now)
        usage[tool_name] = history
        store.write_json(_STORE, usage)


def remaining(tool_name: str) -> dict:
    """How many runs are left in each configured window — for showing the user."""
    limits = _limits_for(tool_name)
    if not limits:
        return {}

    now = time.time()
    with _lock:
        history = _load().get(tool_name, [])

    left = {}
    for window_name, seconds in WINDOWS.items():
        ceiling = limits.get(window_name)
        if ceiling is not None:
            used = sum(1 for stamp in history if now - stamp < seconds)
            left[window_name] = max(0, ceiling - used)
    return left
