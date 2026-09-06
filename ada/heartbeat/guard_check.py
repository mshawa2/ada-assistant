"""Asking, occasionally, about the positions nothing can alert on.

47 of 48 holdings have no stop, no trailing stop and no thesis. Every rule in
`positions.evaluate()` except the earnings warning needs one of those to exist,
so for those 47 the entire alerting machine is switched off — Ada watches them
in the sense that a smoke detector with no battery watches a kitchen.

The obvious implementation is the wrong one. Listing all 47 hands the user a
chore, and a chore is declined once and then ignored forever; raising 47
notices would bury every real alert underneath them. So this asks about
**exactly one position at a time**, largest first, then waits days before
asking again and a month before asking about the same one twice.

**It offers arithmetic, never a number to use.** A stop is a claim about how
much you are willing to lose and where your thesis breaks — Ada knows neither.
What she can do is the sum the user would otherwise do in their head: here is
what you paid, here is what it trades at, here is what a 10%, 15% or 20% stop
comes to. Saying "set it at $58.20" would dress a guess as advice, and a stop
taken on someone else's authority gets moved the first time it is tested, which
is worse than having none.

**Asked, not answered, is still progress.** A position the user decides needs
no stop should stop being raised, and the way to say so is to write a thesis —
itself the more useful artefact. Either answer removes it from the list, which
is the point.
"""

import time

from ada import market, news, store
from ada.heartbeat.notices import add_notice

CHECK_NAME = "unguarded"

_STATE = store.DATA_DIR / "guard_state.json"

# Long gaps on purpose. This is a nudge about something that has been true for
# months and will still be true next week; asking daily would train the user to
# dismiss it without reading.
_DEFAULT_EVERY = 3 * 86400
_DEFAULT_COOLDOWN = 30 * 86400


def _stop_options(price: float) -> str:
    return ", ".join(f"{pct}% = ${price * (1 - pct / 100):,.2f}" for pct in (10, 15, 20))


def check_unguarded(params: dict) -> None:
    state = store.read_json(_STATE, {})
    now = time.time()

    every = params.get("every_seconds", _DEFAULT_EVERY)
    if now - (state.get("last") or 0) < every:
        return

    bare = news.unguarded()
    if not bare:
        # Nothing to ask about. Recording the pass means that if the user later
        # opens a new unguarded position, the next nudge waits a full interval
        # rather than arriving the moment it appears.
        state["last"] = now
        store.write_json(_STATE, state)
        return

    asked = state.get("asked") or {}
    cooldown = params.get("cooldown_seconds", _DEFAULT_COOLDOWN)

    # Largest first, skipping anyone asked about recently. `unguarded()` is
    # already ordered by position value.
    target = next(
        (p for p in bare if now - (asked.get((p.get("symbol") or "").upper()) or 0) > cooldown),
        None,
    )
    if target is None:
        # Everyone has been asked lately. Wait rather than starting over.
        state["last"] = now
        store.write_json(_STATE, state)
        return

    symbol = (target.get("symbol") or "").upper()
    cost = target.get("cost")
    price = target.get("last_price")
    if not price:
        try:
            price = (market.quote(symbol, "5d") or {}).get("price")
        except Exception:
            price = None

    detail = []
    if cost:
        detail.append(f"cost ${cost:,.2f}")
    if price:
        detail.append(f"now ${price:,.2f}")
        detail.append(f"a stop would be {_stop_options(price)}")
    tail = f" ({'; '.join(detail)})" if detail else ""

    add_notice(
        CHECK_NAME,
        f"{symbol} has no stop and no thesis, so nothing can alert on it{tail}. "
        "Set one, or write a line on why you hold it — either stops me asking. "
        f"{len(bare)} holdings are in the same state.",
        level=params.get("level", "normal"),
    )

    asked[symbol] = now
    state["asked"] = asked
    state["last"] = now
    store.write_json(_STATE, state)
