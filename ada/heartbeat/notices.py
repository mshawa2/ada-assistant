"""The dismissible log of things the heartbeat has noticed. `level` controls
how loudly a notice gets surfaced: 'normal' only ever shows up in the calm
log; 'urgent' gets announced at the next session start (unless quiet hours
are active, in which case it's held, not dropped); 'critical' always
interrupts, quiet hours or not.
"""

import time

from ada import audit, store

_STORE = store.DATA_DIR / "heartbeat_notices.json"
_lock = store.lock_for(_STORE)

LEVELS = ("normal", "urgent", "critical")


def _load() -> list[dict]:
    return store.read_json(_STORE, [])


def _save(notices: list[dict]) -> None:
    store.write_json(_STORE, notices)


def add_notice(check: str, text: str, level: str = "normal") -> dict:
    if level not in LEVELS:
        raise ValueError(f"Unknown notice level '{level}'.")
    with _lock:
        notices = _load()
        new_id = max((n["id"] for n in notices), default=0) + 1
        notice = {
            "id": new_id,
            "check": check,
            "text": text,
            "level": level,
            "created_at": time.time(),
            "dismissed": False,
        }
        notices.append(notice)
        _save(notices)
        audit.log("heartbeat_notice", check=check, level=level, text=text)
        return notice


def list_notices(include_dismissed: bool = False) -> list[dict]:
    with _lock:
        notices = _load()
    if not include_dismissed:
        notices = [n for n in notices if not n["dismissed"]]
    return notices


def dismiss_notice(notice_id: int) -> bool:
    with _lock:
        notices = _load()
        found = False
        for n in notices:
            if n["id"] == notice_id:
                n["dismissed"] = True
                found = True
        if found:
            _save(notices)
        return found
