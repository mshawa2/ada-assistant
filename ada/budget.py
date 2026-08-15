"""A ceiling on money, not just on actions.

`ada/limits.py` caps how *often* something runs. That's the right protection
against a loop, and no protection at all against one expensive thing — or
against a hundred cheap things that quietly add up. This is the money version.

Two categories exist, and they differ in kind:

**`model`** is real and already spending. Every turn costs tokens, `cost.py`
has always priced them, but the tally lived in memory and died with the
process — so "how much has Ada cost me this month" had no answer and nothing
stopped a bad week. It's now persisted and checked *before* each call, so
hitting the cap means Ada says so rather than quietly billing you.

**`purchases`** has no tool behind it yet, and its default limit is **zero**.
That's deliberate: the rail should exist before the capability, so the day
something can spend your money it is denied by default until you raise the
limit yourself. Any future purchasing tool must call `check()` before acting
and `record()` after — the same shape `send_message` uses for rate limits.

On floats: model costs are fractions of a cent, so rounding to pennies would
erase them. Amounts are stored at full float precision and only rounded for
display. This is a spending *cap*, not double-entry bookkeeping — if it ever
needs to be a real ledger, that's a Decimal and a rewrite.
"""

import time

from ada import settings, store

_STORE = store.DATA_DIR / "spend.json"
_lock = store.lock_for(_STORE)

WINDOWS = {"per_day": 86400, "per_week": 604800, "per_month": 2592000}
_LABELS = {"per_day": "day", "per_week": "week", "per_month": "month"}

MODEL = "model"
PURCHASES = "purchases"


class BudgetExceeded(Exception):
    """Raised when an action would take a category past its cap."""


def currency() -> str:
    return settings.get("budget.currency", "USD") or "USD"


def _symbol() -> str:
    return {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency(), "")


def money(amount: float) -> str:
    """An amount as text, precise enough not to round model costs to nothing."""
    if amount and abs(amount) < 1:
        return f"{_symbol()}{amount:.4f}".rstrip("0").rstrip(".")
    return f"{_symbol()}{amount:,.2f}"


def _limits_for(category: str) -> dict:
    limits = settings.get("budget.limits", {}) or {}
    return limits.get(category, {}) or {}


def _load() -> dict:
    return store.read_json(_STORE, {})


def spent(category: str, seconds: float) -> float:
    """Total spent in `category` over the last `seconds`."""
    now = time.time()
    with _lock:
        entries = _load().get(category, [])
    return sum(entry.get("amount", 0.0) for entry in entries if now - entry.get("at", 0) < seconds)


def check(category: str, amount: float = 0.0) -> None:
    """Raise BudgetExceeded if spending `amount` would breach a cap.

    Called with the amount when it's known ahead of time (a purchase), or with
    0.0 to mean "am I already over?" — which is how the model check works,
    since the cost of a call isn't known until after it returns.
    """
    limits = _limits_for(category)
    if not limits:
        return

    for window_name, seconds in WINDOWS.items():
        cap = limits.get(window_name)
        if cap is None:
            continue

        already = spent(category, seconds)
        # Two different questions. With a known amount: would this specific
        # spend cross the line? With no amount (the model case, where the cost
        # isn't knowable until the call returns): are we already at it? The
        # second has to be >=, or landing exactly on a $5.00 cap would let the
        # next call through and every one after it.
        breached = (already + amount > cap) if amount else (already >= cap)
        if breached:
            label = _LABELS[window_name]
            if amount:
                detail = (
                    f"that would take it to {money(already + amount)}, over the "
                    f"{money(cap)} per {label} limit"
                )
            else:
                detail = f"the limit is {money(cap)} per {label}"
            raise BudgetExceeded(
                f"Spending limit reached for '{category}': {money(already)} used this "
                f"{label} and {detail}. Tell the user plainly and don't retry — they can "
                "raise it in their settings."
            )


def record(category: str, amount: float, note: str = "") -> None:
    """Note that `amount` was actually spent."""
    if amount <= 0:
        return

    limits = _limits_for(category)
    windows = [WINDOWS[name] for name in limits if name in WINDOWS]
    longest = max(windows) if windows else WINDOWS["per_month"]

    now = time.time()
    with _lock:
        ledger = _load()
        # Pruned to the longest window still answerable, so this can't grow
        # without bound the way an append-only log would.
        entries = [e for e in ledger.get(category, []) if now - e.get("at", 0) < longest]
        entries.append({"at": now, "amount": float(amount), "note": note})
        ledger[category] = entries
        store.write_json(_STORE, ledger)


def summary() -> str:
    """What's been spent against each cap, for showing the user."""
    limits = settings.get("budget.limits", {}) or {}

    lines = []
    for category, windows in limits.items():
        for window_name, cap in (windows or {}).items():
            # `null` disables a window. Settings merge additively, so an empty
            # block can't remove an inherited default — setting the cap to null
            # is how you turn one off, and `check` already skips those.
            if window_name not in WINDOWS or cap is None:
                continue
            used = spent(category, WINDOWS[window_name])
            label = _LABELS[window_name]
            headroom = "at the limit" if used >= cap else f"{money(cap - used)} left"
            lines.append(f"{category}: {money(used)} of {money(cap)} this {label} ({headroom})")

    return "\n".join(lines) if lines else "No spending limits are configured."
