"""The heartbeat engine: a background loop, separate from the conversation
loop, that wakes on an interval and runs any checks that are due.

Checks run sequentially within this one thread, which — combined with each
check's own next_due_at only being pushed forward *after* it returns — is
what guarantees a slow check can never overlap with itself or pile up runs.
"""

import threading
import time

from ada import jobs
from ada.heartbeat import notices, singleton
from ada.heartbeat.checks import CHECKS
from ada.heartbeat.config import load_config
from ada.heartbeat.kill_switch import is_paused
from ada.heartbeat.state import get_check_state, update_check_state


def _run_check(name: str, func, params: dict) -> None:
    try:
        func(params)
    except Exception as e:
        print(f"\n[heartbeat: '{name}' check failed: {e}]", flush=True)
    update_check_state(name, next_due_at=time.time() + params.get("interval_seconds", 3600))


def _run_due_checks(config: dict) -> None:
    if is_paused():
        return
    now = time.time()
    for name, params in config.get("checks", {}).items():
        if not params.get("enabled", True) or name not in CHECKS:
            continue
        next_due_at = get_check_state(name).get("next_due_at", 0)
        if now >= next_due_at:
            _run_check(name, CHECKS[name], params)

    # Anything held back by quiet hours gets retried here, so a notice
    # suppressed at 3am goes out when the window ends rather than waiting for
    # the next time you happen to start a session.
    try:
        notices.flush_pending_pushes()
    except Exception as e:
        print(f"\n[heartbeat: couldn't flush held notifications: {e}]", flush=True)


def start(stop_event: threading.Event) -> threading.Thread:
    if not singleton.acquire():
        owner = singleton.owner_pid()
        print(
            f"[heartbeat: already running in another process{f' (pid {owner})' if owner else ''}"
            " — this one won't duplicate its checks]",
            flush=True,
        )
        return _idle_thread(stop_event)

    # Whoever owns the heartbeat owns the executor, so this is where abandoned
    # work is recovered: a job left `running` inside a process that died — a
    # closed laptop, a crash — goes back on the queue before anything claims
    # again. Safe to do here precisely because the lock above means one process
    # is doing it.
    recovered = jobs.reap_orphans()
    if recovered:
        print(
            f"[heartbeat: put {len(recovered)} interrupted job"
            f"{'s' if len(recovered) != 1 else ''} back on the queue]",
            flush=True,
        )

    # Run one catch-up pass synchronously before handing off to the background
    # thread, so anything overdue is already in the notice log by the time the
    # caller builds its startup greeting — no race with the first tick.
    _run_due_checks(load_config())

    def loop() -> None:
        while not stop_event.is_set():
            # Reloaded each tick rather than captured once at startup: editing
            # an interval or a quiet-hours window in heartbeat_config.json now
            # takes effect on the next tick instead of needing a restart.
            config = load_config()
            stop_event.wait(config.get("tick_seconds", 30))
            if not stop_event.is_set():
                _run_due_checks(config)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def _idle_thread(stop_event: threading.Event) -> threading.Thread:
    """A thread that does nothing but honour stop_event, so callers can treat
    'someone else owns the heartbeat' exactly like owning it themselves."""
    thread = threading.Thread(target=stop_event.wait, daemon=True)
    thread.start()
    return thread
