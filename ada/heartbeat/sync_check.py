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

    if result.get("missing"):
        add_notice(
            "portfolio",
            f"Your broker no longer reports {', '.join(result['missing'])}. "
            "Still tracked in case that's a sync fault — tell me if you sold them.",
            level=params.get("level", "urgent"),
        )
