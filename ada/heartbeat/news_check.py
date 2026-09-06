"""One sweep a market morning: what's being written, and what reports soon.

Two jobs on different clocks, in one check because they share a fetch budget.

**Earnings, weekly.** Dates barely move, and the calendar costs a request per
day looked at. Refreshing once a week keeps `earnings_on` filled — which is what
makes `positions.evaluate()` warn *before* a report instead of after — without
spending fourteen requests every heartbeat tick.

**Headlines, daily.** Once per morning, and only for the largest holdings. A
search per few tickers keeps a forty-eight name book inside a free tier;
sweeping every tick would exhaust it in an afternoon and tell the user nothing
they hadn't already read.

**A headline is announced once.** Keyed on symbol plus title, because the same
story is republished for days and notices are deliberately not deduplicated —
the mistake `sync_check` made and `ci_check` was built to avoid. The state file
remembers titles already surfaced and forgets them once they stop circulating,
so a story that resurfaces months later is news again.

**Nothing here interprets.** The check says something is being written about a
position; deciding what it means is `analyze_ticker`'s job. A model call is not
something to spend unattended on a headline that might be a listicle.
"""

import time

from ada import news
from ada.heartbeat.notices import add_notice

CHECK_NAME = "holdings_news"

_DAY = 86400
_WEEK = 7 * _DAY


def check_holdings_news(params: dict) -> None:
    state = news.load_state()
    now = time.time()
    changed = False

    # --- earnings, weekly ---------------------------------------------------
    if now - (state.get("earnings_refreshed") or 0) > params.get("earnings_every_seconds", _WEEK):
        found = news.refresh_earnings()
        state["earnings_refreshed"] = now
        state["earnings_found"] = len(found)
        changed = True
        # No notice raised here. `positions_check` already owns the "reports in
        # N days" warning and now has dates to work with — saying it twice,
        # from two different checks, is worse than saying it once.

    # --- headlines, daily ---------------------------------------------------
    if now - (state.get("swept") or 0) > params.get("sweep_every_seconds", _DAY):
        covered = news.symbols_held()[: params.get("symbols", 12)]
        items = news.headlines(covered)
        state["swept"] = now
        changed = True

        if items:
            seen = state.get("seen") or {}
            fresh = []
            current = {}
            for item in items:
                symbol = item.get("symbol")
                if not symbol:
                    continue  # unattributed headlines are noise, not news
                key = f"{symbol}|{item['title'][:120]}"
                current[key] = True
                if key not in seen:
                    fresh.append(item)

            # Only titles still circulating are remembered, so the file doesn't
            # grow forever and a story that returns much later counts again.
            state["seen"] = current

            if fresh:
                first = fresh[0]
                extra = f" (+{len(fresh) - 1} more)" if len(fresh) > 1 else ""
                add_notice(
                    CHECK_NAME,
                    f"{first['symbol']}: {first['title']}{extra} "
                    "— headline, not advice; ask for the detail if it matters.",
                    level=params.get("level", "normal"),
                )

    if changed:
        news.save_state(state)
