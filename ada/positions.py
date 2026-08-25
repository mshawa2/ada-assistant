"""What you hold, what you said you'd do about it, and when that's happened.

The useful thing an agent can do for a small account is **not** tell you when
to sell. It's hold you to what you decided when you were calm. You set a stop,
a target and a reason you'd be wrong; weeks later the price is moving and you
are not calm, and the value here is a notification that says "this is the level
you chose" — not an opinion arriving at the worst possible moment.

So every alert in this module is arithmetic against a number *you* gave. The
indicators in `ada/market.py` are carried along as context and are deliberately
never a trigger: RSI crossing 70 raises nothing, because it predicts nothing.

## Nothing here can trade

There is no order path, no broker write, no "sell" tool. The most this can do
is put a notification in front of you. That's a design decision rather than an
unfinished edge — an agent that can act on a price alert unattended is a
different and much worse product than one that taps you on the shoulder.

## Alerts fire once, and re-arm when the condition clears

Same shape as `reminders`' `due_notified`. A stop that's been hit is still hit
tomorrow, and re-sending it every fifteen minutes is how someone learns to
ignore the one alert that mattered. Each kind is flagged when it fires and
un-flagged when the condition stops being true.
"""

import time

from ada import store

_STORE = store.DATA_DIR / "positions.json"
_lock = store.lock_for(_STORE)

# What a rule can raise, and how loudly. `critical` interrupts quiet hours;
# `urgent` is announced at the next session and pushed; `normal` only ever
# shows up in the log. A stop being hit is the one thing here worth waking
# someone for — it's the level they said they'd get out at.
LEVELS = {
    "stop": "critical",
    "target": "urgent",
    "trail": "urgent",
    "earnings": "urgent",
    "context": "normal",
}

# How far below the high-water mark before a trailing alert fires, when the
# position doesn't set its own.
DEFAULT_TRAIL_PCT = 15.0

# Earnings are the one scheduled event that reliably moves a single name, and
# the one thing a holder should decide about *before* it happens.
EARNINGS_WARN_DAYS = 5


def _load() -> list[dict]:
    return store.read_json(_STORE, [])


def _save(positions: list[dict]) -> None:
    store.write_json(_STORE, positions)


def load_positions() -> list[dict]:
    with _lock:
        return _load()


def get(symbol: str) -> dict | None:
    symbol = (symbol or "").strip().upper()
    with _lock:
        return next((p for p in _load() if p["symbol"] == symbol), None)


def require(symbol: str) -> dict:
    position = get(symbol)
    if position is None:
        raise ValueError(f"You don't have a position in {(symbol or '').upper()}.")
    return position


def add(
    symbol: str,
    shares: float,
    cost: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    thesis: str = "",
    invalidation: str = "",
    trail_pct: float | None = None,
    earnings_on: float | None = None,
    source: str = "manual",
) -> dict:
    """Record a holding. Replaces an existing one for the same symbol."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ValueError("A position needs a symbol.")
    if shares is None or float(shares) <= 0:
        raise ValueError("A position needs a positive number of shares.")
    if stop is not None and cost is not None and stop >= cost:
        # Not forbidden — a stop above cost is a locked-in profit and a real
        # thing to want. Worth saying out loud, because it's also what a
        # transposed pair of numbers looks like.
        pass

    with _lock:
        positions = [p for p in _load() if p["symbol"] != symbol]
        position = {
            "symbol": symbol,
            "shares": float(shares),
            "cost": float(cost) if cost is not None else None,
            "stop": float(stop) if stop is not None else None,
            "target": float(target) if target is not None else None,
            "trail_pct": float(trail_pct) if trail_pct is not None else None,
            "thesis": thesis.strip(),
            "invalidation": invalidation.strip(),
            "earnings_on": earnings_on,
            "high_water": None,
            "last_price": None,
            "priced_at": None,
            "source": source,
            "opened_at": time.time(),
            "fired": {},
        }
        positions.append(position)
        _save(positions)
        return position


def update(symbol: str, **fields) -> dict:
    """Change what you decided. Clears the flag for anything you moved.

    Moving a stop is a new decision, and the alert for the old one has already
    been sent — without clearing it, raising your stop to a level the price is
    already below would stay silent forever.
    """
    symbol = (symbol or "").strip().upper()
    allowed = {"shares", "cost", "stop", "target", "trail_pct", "thesis", "invalidation", "earnings_on"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Can't set {', '.join(sorted(unknown))} on a position.")

    with _lock:
        positions = _load()
        position = next((p for p in positions if p["symbol"] == symbol), None)
        if position is None:
            raise ValueError(f"You don't have a position in {symbol}.")

        for key, value in fields.items():
            if value is None:
                continue
            position[key] = value.strip() if isinstance(value, str) else float(value)
            if key in ("stop", "target"):
                position.setdefault("fired", {}).pop(key, None)
            if key == "trail_pct":
                position.setdefault("fired", {}).pop("trail", None)
            if key == "earnings_on":
                position.setdefault("fired", {}).pop("earnings", None)

        _save(positions)
        return position


def remove(symbol: str) -> dict:
    symbol = (symbol or "").strip().upper()
    with _lock:
        positions = _load()
        position = next((p for p in positions if p["symbol"] == symbol), None)
        if position is None:
            raise ValueError(f"You don't have a position in {symbol}.")
        _save([p for p in positions if p["symbol"] != symbol])
        return position


# What a broker knows, and may therefore overwrite on every sync. Everything
# else on a position is either your decision or Ada's bookkeeping, and a sync
# has no business touching either.
BROKER_OWNED = ("shares", "cost", "account_id", "source")


def merge_from_broker(rows: list[dict], source: str = "snaptrade") -> dict:
    """Fold the broker's view into Ada's, leaving your decisions alone.

    `add()` replaces a whole record, which is right when you're telling Ada
    about something for the first time and catastrophic on every sync after —
    it would erase the stop you set last week, along with the thesis, the
    invalidation, the high-water mark and the fired flags, silently, on a
    schedule.

    So only `BROKER_OWNED` fields move. What comes back is what changed, in the
    terms a person cares about:

    - `added`   — the broker reports it, Ada didn't know. Has no rules yet, so
                  it is inert until you set them; the caller says so out loud.
    - `updated` — share count or cost moved.
    - `missing` — Ada tracks it, the broker no longer reports it. **Not
                  deleted.** A sync that hiccups and drops a position carrying a
                  stop would remove an alarm without telling anyone.
    """
    seen = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        # One symbol can be held in several accounts. Ada tracks a symbol, not
        # a lot, so the shares add up and the cost is share-weighted.
        if symbol in seen:
            existing = seen[symbol]
            total = existing["shares"] + float(row.get("shares") or 0)
            if existing.get("cost") and row.get("cost") and total:
                existing["cost"] = (
                    existing["cost"] * existing["shares"] + float(row["cost"]) * float(row["shares"])
                ) / total
            existing["shares"] = total
            existing["account_id"] = "multiple"
        else:
            seen[symbol] = {
                "shares": float(row.get("shares") or 0),
                "cost": float(row["cost"]) if row.get("cost") not in (None, "") else None,
                "account_id": row.get("account_id"),
            }

    added, updated = [], []

    with _lock:
        held = _load()
        by_symbol = {p["symbol"]: p for p in held}

        for symbol, incoming in seen.items():
            position = by_symbol.get(symbol)

            if position is None:
                position = {
                    "symbol": symbol,
                    "shares": incoming["shares"],
                    "cost": incoming["cost"],
                    "stop": None,
                    "target": None,
                    "trail_pct": None,
                    "thesis": "",
                    "invalidation": "",
                    "earnings_on": None,
                    "high_water": None,
                    "last_price": None,
                    "priced_at": None,
                    "account_id": incoming["account_id"],
                    "source": source,
                    "opened_at": time.time(),
                    "fired": {},
                }
                held.append(position)
                added.append(symbol)
                continue

            changed = (
                abs(position.get("shares", 0) - incoming["shares"]) > 1e-9
                or (incoming["cost"] is not None and position.get("cost") != incoming["cost"])
            )
            position["shares"] = incoming["shares"]
            if incoming["cost"] is not None:
                position["cost"] = incoming["cost"]
            position["account_id"] = incoming["account_id"]
            position["source"] = source
            if changed:
                updated.append(symbol)

        # Only positions the broker owns can go missing. One you typed in
        # yourself was never the broker's to report.
        missing = [
            p["symbol"]
            for p in held
            if p["symbol"] not in seen and p.get("source") == source
        ]

        _save(held)

    return {"added": sorted(added), "updated": sorted(updated), "missing": sorted(missing)}


def record_reading(symbol: str, price: float) -> dict:
    """Store the latest price and push the high-water mark up if it moved.

    The high-water mark only ever rises. That's what makes a trailing stop
    trailing — if it tracked the price down, it would never fire.
    """
    with _lock:
        positions = _load()
        position = next((p for p in positions if p["symbol"] == symbol), None)
        if position is None:
            return {}
        position["last_price"] = price
        position["priced_at"] = time.time()
        if position.get("high_water") is None or price > position["high_water"]:
            position["high_water"] = price
        _save(positions)
        return position


def set_fired(symbol: str, kind: str, fired: bool) -> None:
    """Flag or un-flag one kind of alert for one position."""
    with _lock:
        positions = _load()
        position = next((p for p in positions if p["symbol"] == symbol), None)
        if position is None:
            return
        flags = position.setdefault("fired", {})
        if fired:
            flags[kind] = True
        else:
            flags.pop(kind, None)
        _save(positions)


# --- the rules --------------------------------------------------------------


def is_managed(position: dict) -> bool:
    """Whether you have actually decided anything about this holding.

    This mattered the moment positions started arriving by themselves. Adding
    one by hand is a decision; forty-six turning up from a brokerage sync is
    not, and the default trailing rule would have alerted on all of them —
    making `holdings_needing_rules` and the sync message, which both promise
    a bare holding "will never alert", into lies.

    A holding becomes managed when you give it a stop, a target, an explicit
    trailing percentage, or an earnings date. Until then Ada tracks it, prices
    it, shows it, and stays quiet about it.
    """
    return any(
        position.get(field) is not None
        for field in ("stop", "target", "trail_pct", "earnings_on")
    )


def evaluate(position: dict, reading: dict, now: float | None = None) -> list[dict]:
    """Which of this position's rules are true right now.

    Pure: it reads a position and a price and returns findings. Deciding
    whether a finding is *new* — and telling anyone about it — belongs to the
    caller, so this can be tested without a store or a notifier.
    """
    now = time.time() if now is None else now
    price = reading["price"]
    high_water = max(position.get("high_water") or price, price)
    found = []

    stop = position.get("stop")
    if stop is not None and price <= stop:
        found.append(
            {
                "kind": "stop",
                "text": (
                    f"{position['symbol']} is at {price:.2f}, at or below the {stop:.2f} "
                    "stop you set."
                ),
            }
        )

    target = position.get("target")
    if target is not None and price >= target:
        found.append(
            {
                "kind": "target",
                "text": f"{position['symbol']} is at {price:.2f}, at the {target:.2f} target you set.",
            }
        )

    # Only for a holding you've actually decided something about — see
    # `is_managed`. The default would otherwise turn every synced position into
    # an alert, which is exactly what the sync tells you won't happen.
    trail = position.get("trail_pct") or DEFAULT_TRAIL_PCT
    if is_managed(position) and high_water and price < high_water:
        drop = (high_water - price) / high_water * 100
        if drop >= trail:
            found.append(
                {
                    "kind": "trail",
                    "text": (
                        f"{position['symbol']} is {drop:.1f}% below its {high_water:.2f} high "
                        f"since you bought it — past the {trail:.0f}% you set."
                    ),
                }
            )

    earnings = position.get("earnings_on")
    if earnings:
        days = (earnings - now) / 86400
        if 0 <= days <= EARNINGS_WARN_DAYS:
            found.append(
                {
                    "kind": "earnings",
                    "text": (
                        f"{position['symbol']} reports earnings in {days:.0f} day"
                        f"{'' if 0.5 <= days < 1.5 else 's'}. Decide before it happens, not after."
                    ),
                }
            )

    return found


def context(position: dict, reading: dict) -> str | None:
    """Anything worth *noting* rather than alerting on.

    Never a trigger, and worded so it can't be read as one. If the day this
    module starts saying "overbought — consider selling", it has stopped being
    useful and started being a tipster.
    """
    notes = []
    rsi = reading.get("rsi14")
    if rsi is not None and rsi >= 70:
        notes.append(f"RSI {rsi:.0f}, high end of its own range")
    elif rsi is not None and rsi <= 30:
        notes.append(f"RSI {rsi:.0f}, low end of its own range")

    above = reading.get("above_sma50_pct")
    if above is not None and abs(above) >= 10:
        notes.append(f"{above:+.0f}% vs its 50-day average")

    return "; ".join(notes) or None


def summarize(position: dict, reading: dict | None) -> str:
    """One position as a line, with whatever's actually known about it."""
    symbol = position["symbol"]
    if reading is None:
        return f"{symbol} — {position['shares']:g} shares (no price available)"

    price = reading["price"]
    bits = [f"{symbol} {position['shares']:g} @ {price:.2f}"]

    cost = position.get("cost")
    if cost:
        pnl = (price - cost) * position["shares"]
        bits.append(f"{(price - cost) / cost * 100:+.1f}% ({pnl:+.2f})")

    rails = []
    if position.get("stop"):
        rails.append(f"stop {position['stop']:.2f}")
    if position.get("target"):
        rails.append(f"target {position['target']:.2f}")
    if rails:
        bits.append(", ".join(rails))

    return " · ".join(bits)
