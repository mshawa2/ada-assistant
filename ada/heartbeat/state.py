"""Per-check durable state: when each check is next due, plus whatever
custom bookkeeping a check needs (e.g. which files it's already seen).
Survives restarts so the schedule doesn't reset every time the app starts.
"""

from ada import store

_STATE_FILE = store.DATA_DIR / "heartbeat_state.json"
_lock = store.lock_for(_STATE_FILE)


def _load() -> dict:
    return store.read_json(_STATE_FILE, {})


def _save(state: dict) -> None:
    store.write_json(_STATE_FILE, state)


def get_check_state(name: str) -> dict:
    with _lock:
        return _load().get(name, {})


def update_check_state(name: str, **updates) -> None:
    with _lock:
        state = _load()
        state.setdefault(name, {}).update(updates)
        _save(state)
