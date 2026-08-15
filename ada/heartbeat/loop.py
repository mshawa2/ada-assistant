"""The heartbeat engine: a background loop, separate from the conversation
loop, that wakes on an interval and runs any checks that are due.

Checks run sequentially within this one thread, which — combined with each
check's own next_due_at only being pushed forward *after* it returns — is
what guarantees a slow check can never overlap with itself or pile up runs.
"""

import threading
import time

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


def start(stop_event: threading.Event) -> threading.Thread:
    config = load_config()

    # Run one catch-up pass synchronously before handing off to the background
    # thread, so anything overdue is already in the notice log by the time the
    # caller builds its startup greeting — no race with the first tick.
    _run_due_checks(config)

    def loop() -> None:
        while not stop_event.is_set():
            stop_event.wait(config.get("tick_seconds", 30))
            if not stop_event.is_set():
                _run_due_checks(config)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
