"""Working out the options for a stop, and what each one would actually cost.

A stop is a different instrument depending on where you are. Below your cost it
limits a loss; above it, it protects a gain — and the second case is the one
people set badly, because the number that matters is not "how far down is this"
but **"how much of my gain does this level keep"**.

So nothing here recommends a level. It lays out the candidates a holding
actually has — its moving averages, round percentages, its cost, its 52-week
low — and against each one puts the two facts that decide it:

- **what it would bank**: `(level - cost) x shares`, the realised P&L if it fires
- **whether it's already breached**: a level above the current price fires the
  moment it's set, which is a thing to know before setting it and not after

That's the whole idea. The holder picks; this makes the picking informed.

## Fixed or trailing

Named as a question rather than answered. A fixed stop on a position up 481%
is a decision that has to be re-made every time it moves; a trailing one
ratchets and doesn't. But a trail is worse when a position is volatile by
nature or when you have a specific level in mind for a specific reason, so the
module reports which situation a holding is in and leaves the choice alone.

## No model call

All of it is arithmetic over a price and a position. An analysis you have to
pay for and wait on is one you'll skip, and this is meant to be the thing that
happens every time you look at a holding.
"""

from ada import market, positions

# Above this gain a stop is mostly protecting profit rather than capping a
# loss, which changes which instrument fits. Not a rule, a description.
WINNER_PCT = 20.0

# Round levels worth showing alongside the ones the chart supplies.
STEPS = (10, 15, 20, 25)


def _level(name: str, price: float, position: dict, current: float) -> dict:
    """One candidate stop, with the two facts that decide it."""
    shares = position["shares"]
    cost = position.get("cost")
    return {
        "name": name,
        "price": price,
        "away_pct": (price - current) / current * 100,
        # A stop at or above the current price fires immediately. Worth knowing
        # before it's set rather than from the notification a second later.
        "breached": price >= current,
        "banks": (price - cost) * shares if cost else None,
    }


def levels(position: dict, reading: dict) -> list[dict]:
    """Every candidate stop this holding actually has, nearest first."""
    price = reading["price"]
    found = []

    for name, value in (
        ("50-day average", reading.get("sma50")),
        ("200-day average", reading.get("sma200")),
        ("52-week low", reading.get("year_low")),
        ("your cost", position.get("cost")),
    ):
        if value:
            found.append(_level(name, float(value), position, price))

    for step in STEPS:
        found.append(_level(f"{step}% below", price * (1 - step / 100), position, price))

    # Nearest the current price first: that's the order someone reads them in,
    # and the far ones are rarely the answer.
    found.sort(key=lambda level: -level["price"])
    return found


def shape(position: dict, reading: dict) -> dict:
    """What kind of situation this holding is in, before any level is chosen."""
    price = reading["price"]
    cost = position.get("cost")
    gain_pct = ((price - cost) / cost * 100) if cost else None
    high = position.get("high_water") or price

    return {
        "symbol": position["symbol"],
        "price": price,
        "value": price * position["shares"],
        "gain_pct": gain_pct,
        "gain": (price - cost) * position["shares"] if cost else None,
        # A winner's stop protects profit; an underwater one caps a loss. The
        # distinction decides which instrument fits, so it's stated rather
        # than left for the reader to infer from a percentage.
        "winner": gain_pct is not None and gain_pct >= WINNER_PCT,
        "underwater": gain_pct is not None and gain_pct < 0,
        "rsi": reading.get("rsi14"),
        "from_high_pct": reading.get("from_year_high_pct"),
        "vs_sma50_pct": reading.get("above_sma50_pct"),
        "below_sma50": reading.get("sma50") is not None and price < reading["sma50"],
        "high_water": high,
        "managed": positions.is_managed(position),
    }


def trail_price(position: dict, pct: float) -> float:
    """Where a trailing rule would fire today, from the high since buying."""
    return (position.get("high_water") or position.get("last_price") or 0) * (1 - pct / 100)


def describe(position: dict, reading: dict) -> str:
    """The options for one holding, as something a person reads and decides on."""
    s = shape(position, reading)
    out = [f"{s['symbol']} — {position['shares']:g} at {s['price']:.2f}, worth {s['value']:,.0f}"]

    if s["gain_pct"] is not None:
        out.append(f"Cost {position['cost']:.2f} · {s['gain_pct']:+.0f}% ({s['gain']:+,.0f})")

    context = []
    if s["rsi"] is not None:
        context.append(f"RSI {s['rsi']:.0f}")
    if s["from_high_pct"] is not None:
        context.append(f"{s['from_high_pct']:+.1f}% vs its 52-week high")
    if s["vs_sma50_pct"] is not None:
        context.append(f"{s['vs_sma50_pct']:+.1f}% vs its 50-day")
    if context:
        out.append(" · ".join(context))

    if s["underwater"]:
        out.append("\nThis one is below cost, so a stop caps a loss. Any level above "
                   f"{s['price']:.2f} would fire immediately.")
    elif s["winner"]:
        out.append(f"\nUp {s['gain_pct']:.0f}%, so a stop here protects a gain rather than "
                   "capping a loss — the number that matters is what each level keeps, not "
                   "how far down it is.")

    out.append("\nLevels, and what each would bank if it fired:")
    for level in levels(position, reading):
        line = f"  {level['price']:>9,.2f}  {level['name']:<16} {level['away_pct']:+6.1f}%"
        if level["banks"] is not None:
            line += f"   banks {level['banks']:+,.0f}"
        if level["breached"]:
            line += "   ALREADY BELOW THIS — would fire at once"
        out.append(line)

    if s["winner"]:
        out.append(f"\nOr a trailing rule, from its {s['high_water']:.2f} high since you bought:")
        for pct in (10, 15, 20, 25):
            out.append(f"  {trail_price(position, pct):>9,.2f}  {pct}% trail")
        out.append("A trail ratchets up as it rises; a fixed stop is a decision you re-make "
                   "each time it moves.")

    if not s["managed"]:
        out.append("\nNothing set on this yet, so it will never alert.")
    return "\n".join(out)


def unguarded() -> list[dict]:
    """Holdings with no rules, most money at stake first.

    A list of names doesn't say which one to do first. Sorted by what's
    actually exposed, it does.
    """
    at_risk = []
    for held in positions.load_positions():
        if positions.is_managed(held) or not held.get("last_price"):
            continue
        at_risk.append(
            {
                "symbol": held["symbol"],
                "value": held["last_price"] * held["shares"],
                "gain": ((held["last_price"] - held["cost"]) * held["shares"]) if held.get("cost") else None,
            }
        )
    at_risk.sort(key=lambda h: -h["value"])
    return at_risk


def options_for(symbol: str) -> str:
    """One holding's options, priced live."""
    position = positions.require(symbol)
    return describe(position, market.read(position["symbol"]))
