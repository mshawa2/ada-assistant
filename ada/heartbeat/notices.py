"""The dismissible log of things the heartbeat has noticed. `level` controls
how loudly a notice gets surfaced: 'normal' only ever shows up in the calm
log; 'urgent' gets announced at the next session start (unless quiet hours
are active, in which case it's held, not dropped); 'critical' always
interrupts, quiet hours or not.
"""

import time

from ada import audit, notify, store

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

    # Deliberately outside the lock: a push can go over the network, and
    # holding the store's lock across an HTTP call would stall every other
    # reader of this file for as long as ntfy takes to answer.
    _try_push(notice)
    return notice


def _try_push(notice: dict) -> bool:
    """Push a notice if it's due one, and record that it's been dealt with.

    Marking 'never pushes' as settled too is what stops `flush_pending_pushes`
    from reconsidering every normal notice on every single tick forever.
    Returns whether a push actually went out.
    """
    disposition = notify.push_disposition(notice)
    if disposition == notify.HOLD:
        return False  # held for quiet hours — retried on a later tick, not dropped

    if disposition == notify.NEVER:
        _mark_pushed(notice["id"])
        return False

    if notify.deliver(notice):
        _mark_pushed(notice["id"])
        return True
    return False


def _mark_pushed(notice_id: int) -> None:
    with _lock:
        notices = _load()
        for notice in notices:
            if notice["id"] == notice_id:
                notice["pushed"] = True
        _save(notices)


def flush_pending_pushes() -> int:
    """Retry anything held back, e.g. once quiet hours end. Returns how many
    were sent — the heartbeat calls this every tick."""
    pending = [n for n in list_notices() if not n.get("pushed")]
    return sum(1 for notice in pending if _try_push(notice))


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
