"""Keeping the book current on its own.

**Off by default**, and that isn't timidity. A sync merges into the positions
that drive every alert Ada raises; it's worth watching it do that once by hand
before letting it run while you're asleep. Turn it on in `ada_config.json` once
you've seen a sync you agree with.

Only during market hours, for the same reason `positions_check` is: share counts
don't move when the market is shut, and a failed call at 3am is a notice about
nothing.

It reports **new holdings** and **holdings the broker stopped reporting**, and
nothing else. A share count changing by itself is not news — but a position
arriving with no stop is, because it will be shown and priced and will never
alert, and a position vanishing is, because it might be a sync fault rather than
a sale.
"""

from ada import brokerage, portfolio
from ada.heartbeat.notices import add_notice
from ada.heartbeat.positions_check import market_open
from ada.heartbeat.state import get_check_state, update_check_state

CHECK_NAME = "sync"


def check_sync(params: dict) -> None:
    if not params.get("ignore_market_hours") and not market_open():
        return

    try:
        result = portfolio.sync()
    except brokerage.NotConnected:
        # Nothing is configured. That's a setting, not an incident, and a
        # notice every five minutes about it would be its own problem.
        return
    except brokerage.BrokerageError as e:
        # Said to the console rather than raised as a notice: a flaky endpoint
        # must not look like something happening to your money.
        print(f"\n[portfolio: couldn't sync — {e}]", flush=True)
        return

    if result.get("added"):
        add_notice(
            "portfolio",
            f"New in your brokerage: {', '.join(result['added'])}. "
            "No stop and nothing written about what would make you wrong yet, so "
            "they won't alert until you set those.",
            level=params.get("level", "urgent"),
        )

    # `missing` is *permanent state*, not an event: a symbol the broker stopped
    # reporting keeps not being reported, so noticing it on every tick wrote the
    # same sentence into the log every five minutes all day. Only what is newly
    # missing since the last sync is news.
    #
    # `added` needs no such guard — it is an event by construction, since a
    # position that arrived is present on the next sync and so never appears in
    # `added` twice.
    missing = set(result.get("missing") or [])
    already_reported = set(get_check_state(CHECK_NAME).get("reported_missing", []))
    newly_missing = sorted(missing - already_reported)

    # The whole current set is stored, not the union with what came before, so a
    # holding that reappears and later vanishes again is reported again.
    update_check_state(CHECK_NAME, reported_missing=sorted(missing))

    if newly_missing:
        add_notice(
            "portfolio",
            f"Your broker no longer reports {', '.join(newly_missing)}. "
            "Still tracked in case that's a sync fault — tell me if you sold them.",
            level=params.get("level", "urgent"),
        )
