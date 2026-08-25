"""Turning a brokerage's view of what you own into Ada's, without trampling it.

The broker knows what you hold. It does not know why you hold it, where you
decided to get out, or what would tell you the reason had stopped being true —
and those are the only things Ada's alerting layer runs on.

So a sync is a **merge**, not a replace:

| field                                          | owner  | on sync        |
|------------------------------------------------|--------|----------------|
| shares, cost, source, account                   | broker | overwritten    |
| stop, target, trail_pct, thesis, invalidation   | you    | never touched  |
| high_water, fired, priced_at                    | Ada    | preserved      |

`positions.add()` replaces a whole record, which is right when you're telling
Ada about something and wrong on every sync after the first — it would silently
erase the stop you set last week. `positions.merge_from_broker()` exists for
exactly that reason.

## Two rules that look like edge cases and aren't

**A holding the broker stops reporting is not deleted.** It's reported, so you
can say "yes, I sold that". A sync that hiccups and quietly drops a position
which had a stop on it is the worst failure available here — it removes an
alarm without saying so.

**A holding Ada has never seen arrives with no rules**, and is flagged as such.
Without a stop and an invalidation it is inert: it will be priced and shown and
will never alert. Saying so is the difference between tracking something and
only appearing to.
"""

from ada import brokerage, positions, store

_STATE = store.DATA_DIR / "portfolio.json"
_lock = store.lock_for(_STATE)

SOURCE = "snaptrade"


def _cash(balances: list[dict]) -> tuple[float, float]:
    """Total cash and buying power across an account's currencies.

    Multi-currency accounts report one entry per currency. Summing them is a
    simplification, and an honest one only because it's shown next to the
    per-account detail rather than instead of it.
    """
    cash = sum(float(b.get("cash") or 0) for b in balances)
    power = sum(float(b.get("buying_power") or b.get("buyingPower") or 0) for b in balances)
    return cash, power


def _symbol_of(holding: dict) -> str | None:
    """The ticker, out of whichever shape the broker used.

    SnapTrade nests the symbol differently across endpoints and brokerages, so
    this tries the known shapes rather than assuming one. Returning None for an
    unrecognised shape is deliberate — a position keyed by the wrong string
    would look like a new holding forever.
    """
    # Robinhood via /positions/all nests it under `instrument`; other shapes
    # put it under `symbol`, sometimes a string and sometimes another dict.
    # Tried in order rather than assumed, because the wrong key silently yields
    # a position that looks new on every single sync.
    for container in (holding.get("instrument"), holding.get("symbol"), holding):
        if isinstance(container, str):
            return container.strip().upper() or None
        if not isinstance(container, dict):
            continue
        for key in ("symbol", "raw_symbol", "rawSymbol", "ticker"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
            if isinstance(value, dict):
                inner = value.get("symbol") or value.get("raw_symbol") or value.get("rawSymbol")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip().upper()
    return None


def _number(value) -> float | None:
    """A broker's number, whatever it happens to be wrapped in.

    They arrive as strings as often as floats, and sometimes as
    `{"amount": 41230.19, "currency": {...}}` — an account total came back that
    way and put a dict into `float()`. Unwrapping here rather than at each call
    site keeps every reader honest about it.
    """
    if isinstance(value, dict):
        value = value.get("amount", value.get("value"))
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _units(holding: dict) -> float:
    for key in ("units", "quantity", "fractional_units", "open_quantity"):
        value = holding.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def kind_of(holding: dict) -> str:
    """stock, crypto, option — as the broker labels it.

    Worth keeping: `market.py` prices a stock as `NVDA` and a crypto as
    `BTC-USD`, so a position that doesn't say which will be looked up wrong.
    """
    instrument = holding.get("instrument")
    if isinstance(instrument, dict):
        return str(instrument.get("kind") or "stock").lower()
    return "stock"


def _cost(holding: dict) -> float | None:
    for key in ("average_purchase_price", "averagePurchasePrice", "average_price", "cost_basis"):
        value = holding.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def snapshot() -> dict:
    """Read every connected account and everything in it. One live call set.

    Raises `NotConnected` upward untouched — see `brokerage.py` for why an
    absent connection must never be renderable as an empty portfolio.
    """
    found = []
    for account in brokerage.accounts():
        account_id = account.get("id")
        if not account_id:
            continue

        balances = brokerage.balances(account_id)
        cash, power = _cash(balances if isinstance(balances, list) else [])

        rows = []
        for holding in brokerage.positions(account_id):
            symbol = _symbol_of(holding)
            if not symbol:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "shares": _units(holding),
                    "cost": _cost(holding),
                    "price": _number(holding.get("price")),
                    "kind": kind_of(holding),
                    "account_id": account_id,
                }
            )

        found.append(
            {
                "id": account_id,
                "name": account.get("name") or account.get("institution_name") or "Account",
                "institution": account.get("institution_name") or account.get("brokerage") or "",
                "number": brokerage.mask(account.get("number")),
                "cash": cash,
                "buying_power": power,
                "total": _number((account.get("balance") or {}).get("total")),
                "holdings": rows,
            }
        )

    return {"accounts": found, "at": __import__("time").time()}


def sync() -> dict:
    """Pull the real book and merge it into Ada's positions.

    Returns what changed, in the terms a person cares about: what's new, what
    moved, and what the broker no longer reports.
    """
    live = snapshot()
    rows = [row for account in live["accounts"] for row in account["holdings"]]

    result = positions.merge_from_broker(rows, source=SOURCE)
    result["accounts"] = live["accounts"]
    result["at"] = live["at"]

    with _lock:
        store.write_json(_STATE, live)
    return result


def last_sync() -> dict:
    """What the last sync saw. Served to the dashboard, which never calls out."""
    with _lock:
        return store.read_json(_STATE, {"accounts": [], "at": None})


def describe(result: dict) -> str:
    """A sync, as a sentence — including the two things worth acting on."""
    lines = []
    accounts = result.get("accounts") or []
    if accounts:
        total_cash = sum(a["cash"] for a in accounts)
        lines.append(
            f"Synced {len(accounts)} account{'s' if len(accounts) != 1 else ''}, "
            f"{total_cash:,.2f} in cash."
        )

    if result.get("updated"):
        lines.append(f"Updated: {', '.join(result['updated'])}.")

    if result.get("added"):
        lines.append(
            f"New to Ada: {', '.join(result['added'])}. "
            "These have no stop and no invalidation yet, so they'll be priced and shown "
            "but will never alert — worth setting those now."
        )

    if result.get("missing"):
        lines.append(
            f"The broker no longer reports: {', '.join(result['missing'])}. "
            "Still tracked, in case that's a sync hiccup — say so if you've sold them."
        )

    # Said explicitly rather than left implied by the absence of news: "synced,
    # 2,500 in cash" and "synced, 2,500 in cash, and your book is unchanged" are
    # different reports, and only one of them answers the question.
    if not (result.get("added") or result.get("updated") or result.get("missing")):
        lines.append("Nothing changed.")
    return " ".join(lines) if lines else "Synced. Nothing changed."
