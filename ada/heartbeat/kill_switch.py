"""One obvious way to pause all proactive behavior at once, without tearing
anything down. The loop thread keeps ticking while paused — it just skips
running checks — so resuming needs no restart, and anything that was due
while paused still runs (once) on resume rather than being lost.
"""

from ada import store

_FILE = store.DATA_DIR / "heartbeat_paused.json"
_lock = store.lock_for(_FILE)


def is_paused() -> bool:
    with _lock:
        return store.read_json(_FILE, {}).get("paused", False)


def set_paused(paused: bool) -> None:
    with _lock:
        store.write_json(_FILE, {"paused": paused})
