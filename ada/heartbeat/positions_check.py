"""The check that watches your holdings and taps you on the shoulder.

Deterministic on purpose: no model call, no tools, no job. An alert about a
price has to be instant and free, and a stop being hit is arithmetic — sending
it through a language model would add latency, cost, and a chance of it being
paraphrased into something you didn't set.

## Only while the market is open

Prices don't move at 3am, so a check that ran then would either say nothing or
— worse — re-alert on a stale close. It runs on US market hours, and the last
run of the day happens near the close, which is the one moment a holder
actually has a decision to make.

## Nothing here can trade

The most this does is raise a notice. See `ada/positions.py` for why that's the
design rather than a missing feature.
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ada import market, positions
from ada.heartbeat.notices import add_notice

_EXCHANGE = ZoneInfo("America/New_York")
OPEN_MINUTES = 9 * 60 + 30
CLOSE_MINUTES = 16 * 60


def market_open(now: float | None = None) -> bool:
    """Whether US equities are open. Weekends and clock, not holidays.

    Holidays aren't handled and that's a deliberate gap rather than an
    oversight: the failure mode is Ada checking prices on Thanksgiving, seeing
    yesterday's close, and raising nothing new because the alerts already
    fired. A holiday calendar is a dependency to buy a day of silence.
    """
    moment = datetime.fromtimestamp(time.time() if now is None else now, _EXCHANGE)
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return OPEN_MINUTES <= minutes < CLOSE_MINUTES


def check_positions(params: dict) -> None:
    """Read every holding's price and raise anything that's newly true."""
    if not params.get("ignore_market_hours") and not market_open():
        return

    held = positions.load_positions()
    if not held:
        return

    for position in held:
        symbol = position["symbol"]
        try:
            reading = market.read(symbol)
        except market.MarketError as e:
            # A price that can't be read is "don't know", never "nothing to
            # report". Said once at a level that doesn't interrupt, because a
            # flaky endpoint shouldn't look like a stop being hit.
            print(f"\n[positions: couldn't price {symbol} — {e}]", flush=True)
            continue

        positions.record_reading(symbol, reading["price"])
        fresh = positions.get(symbol) or position
        found = {alert["kind"]: alert for alert in positions.evaluate(fresh, reading)}
        already = fresh.get("fired", {})

        for kind, alert in found.items():
            if already.get(kind):
                continue
            note = positions.context(fresh, reading)
            add_notice(
                "positions",
                alert["text"] + (f" ({note})" if note and kind != "earnings" else "")
                + _invalidation_line(fresh, kind),
                level=positions.LEVELS.get(kind, "urgent"),
            )
            positions.set_fired(symbol, kind, True)

        # Un-flag anything that stopped being true, so a stop you move — or a
        # price that recovers — can alert again rather than staying silent.
        for kind in list(already):
            if kind not in found:
                positions.set_fired(symbol, kind, False)


def _invalidation_line(position: dict, kind: str) -> str:
    """Hand back what they said would make them wrong.

    This is the whole point of the feature. The alert arrives when the price is
    moving and the holder is not calm; the sentence they wrote when they *were*
    calm is the most useful thing that can be next to it.
    """
    if kind == "context" or not position.get("invalidation"):
        return ""
    return f' You said you\'d be wrong if: "{position["invalidation"]}"'


def digest(now: float | None = None) -> str:
    """Every holding against its own rules, as text. No alerts, no state.

    Used by the tool and by anything scheduled — a job that asks the model to
    write the evening summary starts from this rather than from raw prices.
    """
    held = positions.load_positions()
    if not held:
        return "You're not holding anything I know about."

    lines, total, known = [], 0.0, 0.0
    for position in held:
        try:
            reading = market.read(position["symbol"])
        except market.MarketError as e:
            lines.append(f"{position['symbol']} — couldn't get a price ({e})")
            continue

        positions.record_reading(position["symbol"], reading["price"])
        fresh = positions.get(position["symbol"]) or position
        line = positions.summarize(fresh, reading)

        note = positions.context(fresh, reading)
        if note:
            line += f" · {note}"

        hits = positions.evaluate(fresh, reading, now=now)
        for alert in hits:
            line += f"\n    → {alert['text']}"
        if hits and fresh.get("invalidation"):
            line += f'\n    → you said you\'d be wrong if: "{fresh["invalidation"]}"'

        lines.append(line)
        if fresh.get("cost"):
            total += (reading["price"] - fresh["cost"]) * fresh["shares"]
            known += fresh["cost"] * fresh["shares"]

    if known:
        lines.append(f"\nOverall: {total:+.2f} on {known:.2f} invested ({total / known * 100:+.1f}%).")
    return "\n".join(lines)
