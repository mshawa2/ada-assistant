"""What is about to happen to the book, and what just did.

Two questions a trader asks every week, and Ada could answer neither cheaply.

**Earnings.** `positions.py` has warned about upcoming earnings since it was
written — `evaluate()` turns `earnings_on` into "reports in 3 days, decide before
it happens, not after". That code has never fired once, because nothing ever
filled the field: 0 of 48 positions have a date. The machinery was built and
starved, so this fills it.

The calendar is fetched **by date, not by ticker**. Nasdaq publishes everyone
reporting on a given day, so two weeks of holdings coverage is fourteen
requests regardless of whether the book holds eight names or eighty. Per-ticker
lookup would be one request each and would get slower exactly as the portfolio
grows.

**News.** Until now the only way Ada saw a headline was `analyze_ticker`, which
runs four analysts and a debate — rate-limited to twelve a day, and rightly so.
That is the correct tool for "should I buy this" and hopeless for "did anything
happen to my forty-eight holdings overnight". This adds the cheap path: one
batched search, headlines only, no model call.

**Why headlines and not article bodies.** A headline is enough to decide whether
to look, and pulling article text into Ada's context would let anyone who
publishes a page write instructions into it. The same reasoning as email
subjects in `people.py`, and callers carry the same untrusted framing.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from ada import positions, store

_STATE = store.DATA_DIR / "news_state.json"

_CALENDAR = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
_HEADERS = {
    # Nasdaq returns an empty body to a bare urllib user-agent. Not an attempt
    # to look like a browser beyond what the endpoint needs to answer at all —
    # the same trade `market.py` already makes with Yahoo.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "application/json",
}
_TIMEOUT = 20

# Two weeks out. Far enough that a report never arrives unannounced, short
# enough that the sweep stays cheap and the dates stay accurate — companies
# move them, and a date fetched a month early is often wrong by the time it
# matters.
HORIZON_DAYS = 14


def _get(url: str) -> dict | None:
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        # A calendar that can't be reached is not an incident. The caller
        # reports what it managed to gather.
        return None


def earnings_calendar(days: int = HORIZON_DAYS) -> dict[str, tuple[str, str]]:
    """Symbol -> (ISO date, session) for everything reporting in the next `days`.

    Weekends are skipped rather than requested: nobody reports on a Saturday,
    and two wasted round trips a week adds up to nothing useful.
    """
    found: dict[str, tuple[str, str]] = {}
    today = date.today()

    for offset in range(days + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        payload = _get(_CALENDAR.format(day=day.isoformat()))
        rows = ((payload or {}).get("data") or {}).get("rows") or []
        for row in rows:
            symbol = (row.get("symbol") or "").strip().upper()
            if symbol and symbol not in found:
                found[symbol] = (day.isoformat(), row.get("time") or "")
    return found


def session_label(raw: str) -> str:
    # Nasdaq's own vocabulary: time-pre-market, time-after-hours,
    # time-not-supplied. Matching on "before" looked reasonable and silently
    # returned "" for every pre-market report.
    lowered = (raw or "").lower()
    if "pre-market" in lowered or "before" in lowered:
        return "before the open"
    if "after" in lowered:
        return "after the close"
    return ""


def refresh_earnings(days: int = HORIZON_DAYS) -> list[dict]:
    """Stamp `earnings_on` onto every holding that reports soon.

    Returns what was found, soonest first, so a caller can report it without
    re-reading the book.

    Only dates *in the window* are written. A date is not cleared when it falls
    out of the window: a report that already happened is still the last thing
    that happened to that position, and `evaluate()` ignores past dates anyway.
    """
    calendar = earnings_calendar(days)
    if not calendar:
        return []

    found = []
    for position in positions.load_positions():
        symbol = (position.get("symbol") or "").upper()
        entry = calendar.get(symbol)
        if not entry:
            continue
        when, session = entry
        stamp = datetime.fromisoformat(when).replace(tzinfo=timezone.utc).timestamp()
        if position.get("earnings_on") != stamp:
            try:
                positions.update(symbol, earnings_on=stamp)
            except Exception:
                # One symbol the store refuses shouldn't lose the other forty.
                continue
        found.append(
            {
                "symbol": symbol,
                "date": when,
                "session": session_label(session),
                "days": (datetime.fromisoformat(when).date() - date.today()).days,
            }
        )
    found.sort(key=lambda f: f["days"])
    return found


def symbols_held() -> list[str]:
    """Every distinct symbol in the book, biggest holding first.

    Ordered by value because a news budget has to be spent somewhere, and a
    headline about the largest position is worth more than one about the
    smallest.
    """
    by_symbol: dict[str, float] = {}
    for position in positions.load_positions():
        symbol = (position.get("symbol") or "").upper()
        if not symbol:
            continue
        price = position.get("last_price") or position.get("cost") or 0
        by_symbol[symbol] = by_symbol.get(symbol, 0) + (position.get("shares") or 0) * price
    return [s for s, _ in sorted(by_symbol.items(), key=lambda kv: -kv[1])]


def headlines(symbols: list[str], per_query: int = 5) -> list[dict]:
    """Recent headlines for `symbols`, batched into as few searches as possible.

    Symbols are grouped several to a query rather than searched one at a time.
    A book of forty-eight names is then a handful of searches instead of
    forty-eight, which is the difference between a daily sweep that fits inside
    a free tier and one that doesn't.

    Returns [] rather than raising when no provider is configured — a briefing
    without news is still a briefing.
    """
    from ada.tools import search as search_tool

    if not symbols:
        return []
    try:
        provider = search_tool._choose_provider()
        if provider == "wikipedia":
            return []  # an encyclopaedia has no opinion on this morning
        runner = search_tool._PROVIDERS[provider]
    except Exception:
        return []

    out: list[dict] = []
    for start in range(0, len(symbols), 6):
        batch = symbols[start : start + 6]
        query = " OR ".join(f"{s} stock" for s in batch) + " news today"
        try:
            results = runner(query, per_query)
        except Exception:
            continue  # one failed batch shouldn't lose the rest
        for result in results or []:
            title = (result.get("title") or "").strip()
            if not title:
                continue
            upper = title.upper()
            hit = next((s for s in batch if s in upper), None)
            out.append({"symbol": hit, "title": title, "url": result.get("url") or ""})
    return out


def unguarded() -> list[dict]:
    """Holdings with no stop, no trail and no thesis, biggest first.

    Not a nag list. The point is that a position with none of these is one Ada
    can say nothing useful about later: no price that means "wrong", no
    sentence saying what would have to change. Every alert in
    `positions.evaluate()` except earnings needs one of them to exist.
    """
    ranked = symbols_held()
    order = {symbol: index for index, symbol in enumerate(ranked)}
    bare = [
        p
        for p in positions.load_positions()
        if not p.get("stop") and not p.get("thesis") and not p.get("trail_pct")
    ]
    bare.sort(key=lambda p: order.get((p.get("symbol") or "").upper(), 999))
    return bare


def load_state() -> dict:
    return store.read_json(_STATE, {})


def save_state(state: dict) -> None:
    store.write_json(_STATE, state)


def swept_seconds_ago() -> float:
    return time.time() - (load_state().get("swept") or 0)
