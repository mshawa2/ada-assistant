"""Prices, and the arithmetic you can do on them without pretending.

Free and keyless, the same trade `weather.py` makes with Open-Meteo: Yahoo's
public chart endpoint gives a live price, the day and 52-week ranges, and a
year of daily closes in one call. No account, no key, nothing to leak. It's an
undocumented endpoint rather than a product, so it can change — `quote()` fails
with a clear message rather than half-parsing, and everything above it treats a
missing reading as "don't know" instead of "no alert".

## What the indicators are for

They are **context, not signals.** RSI crossing 70 is not a reason to sell, and
nothing in here will say it is. What they're good for is the sentence "this is
a long way above its average and near its 52-week high" — which is worth
knowing when you're deciding, and is not the same thing as being told what to
decide.

The alerts that actually matter are in `ada/positions.py`, and they're all
rules *you* set: your stop, your target, your trailing drawdown. Those are
arithmetic against a number you chose, not a prediction.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ada import store

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_TIMEOUT = 12

# The endpoint returns 403 to a bare urllib user-agent. Not an attempt to look
# like something we aren't — it's a public read of a public page.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


class MarketError(Exception):
    """A price couldn't be read. Distinct from 'the price is fine' on purpose."""


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise MarketError("no such symbol") from e
        raise MarketError(f"the price service returned {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise MarketError(f"couldn't reach the price service ({e})") from e
    except json.JSONDecodeError as e:
        raise MarketError("the price service returned something unreadable") from e


def quote(symbol: str, span: str = "1y") -> dict:
    """A live price plus the history the indicators need, in one call."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise MarketError("no symbol given")

    payload = _get(_CHART_URL.format(symbol=urllib.parse.quote(symbol)) + f"?range={span}&interval=1d")
    result = (payload.get("chart") or {}).get("result")
    if not result:
        error = ((payload.get("chart") or {}).get("error") or {}).get("description")
        raise MarketError(error or f"no price data for {symbol}")

    meta = result[0].get("meta") or {}
    series = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
    closes = [c for c in (series.get("close") or []) if c is not None]

    price = meta.get("regularMarketPrice")
    if price is None:
        raise MarketError(f"no current price for {symbol}")

    return {
        "symbol": meta.get("symbol", symbol),
        "name": meta.get("shortName") or meta.get("longName") or symbol,
        "currency": meta.get("currency", "USD"),
        "price": float(price),
        # NOT meta["chartPreviousClose"] — on a 1-year range that's the close
        # before the whole window, so "today's move" came out as +17% against
        # a price from last August. The prior daily close is the one in the
        # series, second from the end, because the last entry is today.
        "previous_close": closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose"),
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "year_high": meta.get("fiftyTwoWeekHigh"),
        "year_low": meta.get("fiftyTwoWeekLow"),
        "volume": meta.get("regularMarketVolume"),
        "at": meta.get("regularMarketTime"),
        "closes": closes,
    }


# --- the arithmetic, kept away from the network so it can be tested ---------


def sma(closes: list[float], window: int) -> float | None:
    """Simple moving average, or None when there isn't enough history.

    None rather than a shorter average: a "200-day average" computed from
    forty days is a different number wearing the same label, and it would be
    quietly wrong on exactly the young or thinly-traded names where it matters.
    """
    if len(closes) < window or window <= 0:
        return None
    return sum(closes[-window:]) / window


def rsi(closes: list[float], window: int = 14) -> float | None:
    """Wilder's RSI. None when there isn't enough history.

    Wilder's smoothing rather than a plain average of the last 14 changes,
    because that's what every chart the user might compare this against uses,
    and an indicator that disagrees with their broker's is worse than none.
    """
    if len(closes) <= window:
        return None

    gains, losses = [], []
    for earlier, later in zip(closes[-(window + 1) :], closes[-window:]):
        change = later - earlier
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window

    # Every change was up: no loss to divide by, and the index is pinned.
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def pct(a: float | None, b: float | None) -> float | None:
    """`a` as a percentage move from `b`."""
    if a is None or not b:
        return None
    return (a - b) / b * 100


def read(symbol: str) -> dict:
    """A quote with the indicators worked out. One network call."""
    data = quote(symbol)
    closes = data["closes"]
    price = data["price"]

    data["sma20"] = sma(closes, 20)
    data["sma50"] = sma(closes, 50)
    data["sma200"] = sma(closes, 200)
    data["rsi14"] = rsi(closes, 14)
    data["day_change_pct"] = pct(price, data.get("previous_close"))
    data["from_year_high_pct"] = pct(price, data.get("year_high"))
    data["above_sma50_pct"] = pct(price, data["sma50"])
    return data


# A day of closes doesn't change during a day, so it's fetched once and kept.
# Without this, drawing a sparkline on each of forty-six holdings would mean
# forty-six requests every time a dashboard tab was refreshed, to a free service
# that asks nothing of us, for a line that moves once a day.
_HISTORY = store.DATA_DIR / "price_history.json"
_history_lock = store.lock_for(_HISTORY)
HISTORY_TTL = 6 * 3600


def history(symbol: str, days: int = 90) -> list[float]:
    """Recent daily closes for a sparkline, cached.

    Returns an empty list rather than raising: a missing line is a cosmetic
    problem, and a dashboard that 500s because one ticker didn't resolve is a
    worse one.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return []

    now = time.time()
    with _history_lock:
        cache = store.read_json(_HISTORY, {})
        entry = cache.get(symbol)
        if entry and now - entry.get("at", 0) < HISTORY_TTL:
            return entry.get("closes", [])[-days:]

    try:
        closes = quote(symbol, span="6mo")["closes"]
    except MarketError:
        # Remembered as a failure so a delisted or misspelled ticker isn't
        # retried on every single poll.
        closes = []

    with _history_lock:
        cache = store.read_json(_HISTORY, {})
        cache[symbol] = {"at": now, "closes": closes[-days:]}
        store.write_json(_HISTORY, cache)
    return closes[-days:]


def describe(reading: dict) -> str:
    """The indicators as a sentence, framed as what they are.

    Written to be read next to a decision rather than instead of one — hence
    "sitting above", not "bullish", and no verb that tells anyone to do
    anything.
    """
    bits = [f"{reading['symbol']} {reading['currency']} {reading['price']:.2f}"]

    if reading.get("day_change_pct") is not None:
        bits.append(f"{reading['day_change_pct']:+.1f}% today")
    if reading.get("from_year_high_pct") is not None:
        bits.append(f"{reading['from_year_high_pct']:+.1f}% vs its 52-week high")
    if reading.get("above_sma50_pct") is not None:
        bits.append(f"{reading['above_sma50_pct']:+.1f}% vs its 50-day average")
    if reading.get("rsi14") is not None:
        bits.append(f"RSI {reading['rsi14']:.0f}")

    return " · ".join(bits)
