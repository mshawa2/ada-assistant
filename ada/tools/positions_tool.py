"""Recording what you hold and what you decided about it.

**None of these can trade.** There is no order path anywhere in Ada; the most
this reaches is a notification. That's stated in every description here so the
model never implies otherwise to someone who's just been alerted that their
stop was hit and is asking what happens next.

The `invalidation` field is the one that earns its place. A stop is a price and
a price is easy; "I'd be wrong if it closes below the 50-day on volume" is the
thing people mean and never write down, and it's what gets handed back to them
weeks later when the alert fires and they're deciding in a hurry.
"""

from ada import market, positions, when
from ada.heartbeat.positions_check import digest
from ada.tools.registry import tool


@tool(
    name="add_position",
    description=(
        "Record something the user holds, along with the rules they've decided for it. "
        "Use this when they say they bought something, or want Ada to watch a holding.\n\n"
        "Ada cannot buy or sell anything — this only records what they tell you and lets "
        "her alert them later. Never imply an order was placed.\n\n"
        "Always try to capture 'invalidation': what would tell them the reason they "
        "bought it no longer holds. If they haven't said, ask — it's the single most "
        "useful thing to have written down before a price starts moving. A stop price is "
        "not the same thing: one is a level, the other is a reason."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker, e.g. 'NVDA'."},
            "shares": {"type": "number", "description": "How many shares they hold."},
            "cost": {"type": "number", "description": "Average price paid per share, if they said."},
            "stop": {"type": "number", "description": "Price at which they've decided to get out."},
            "target": {"type": "number", "description": "Price at which they've decided to take profit."},
            "trail_pct": {
                "type": "number",
                "description": "Alert if it falls this many percent from its high since buying. Defaults to 15.",
            },
            "thesis": {"type": "string", "description": "Why they bought it, in their words."},
            "invalidation": {
                "type": "string",
                "description": "What would make them wrong, in their words. Ask if they haven't said.",
            },
            "earnings_on": {
                "type": "string",
                "description": "Next earnings date if known, ISO 8601 e.g. '2026-11-19T16:00'.",
            },
        },
        "required": ["symbol", "shares"],
    },
)
def add_position(
    symbol: str,
    shares: float,
    cost: float = 0,
    stop: float = 0,
    target: float = 0,
    trail_pct: float = 0,
    thesis: str = "",
    invalidation: str = "",
    earnings_on: str = "",
) -> str:
    position = positions.add(
        symbol,
        shares,
        cost=cost or None,
        stop=stop or None,
        target=target or None,
        trail_pct=trail_pct or None,
        thesis=thesis,
        invalidation=invalidation,
        earnings_on=when.to_epoch(earnings_on) if earnings_on else None,
    )

    try:
        reading = market.read(position["symbol"])
        now = f" It's at {reading['price']:.2f} now."
    except market.MarketError:
        now = ""

    missing = []
    if not position.get("stop"):
        missing.append("a stop")
    if not position.get("invalidation"):
        missing.append("what would make them wrong")

    ask = f" They haven't given {' or '.join(missing)} — worth asking now rather than mid-move." if missing else ""
    return f"Recorded {position['shares']:g} {position['symbol']}.{now}{ask}"


@tool(
    name="list_positions",
    description=(
        "Show what the user holds, with current prices, profit and loss, and how each sits "
        "against the stop and target they set. Use this when they ask about their portfolio, "
        "positions, holdings, or how something is doing.\n\n"
        "This reads prices; it cannot trade."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_positions() -> str:
    return digest()


@tool(
    name="update_position",
    description=(
        "Change the rules on a holding — move a stop, set a target, revise the thesis, or "
        "note an earnings date. Use this when the user changes their mind about a position "
        "they already hold.\n\n"
        "Moving a stop re-arms its alert, so a stop moved above the current price will fire "
        "again straight away if it's already breached. Ada cannot trade."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker of the position to change."},
            "shares": {"type": "number", "description": "New share count, if it changed."},
            "cost": {"type": "number", "description": "New average cost, if it changed."},
            "stop": {"type": "number", "description": "New stop price."},
            "target": {"type": "number", "description": "New target price."},
            "trail_pct": {"type": "number", "description": "New trailing-drawdown percentage."},
            "thesis": {"type": "string", "description": "Revised reason for holding it."},
            "invalidation": {"type": "string", "description": "Revised reason they'd be wrong."},
            "earnings_on": {"type": "string", "description": "Next earnings date, ISO 8601."},
        },
        "required": ["symbol"],
    },
)
def update_position(symbol: str, **fields) -> str:
    if "earnings_on" in fields and fields["earnings_on"]:
        fields["earnings_on"] = when.to_epoch(fields["earnings_on"])
    changes = {k: v for k, v in fields.items() if v not in (None, "", 0)}
    if not changes:
        raise ValueError("Nothing to change — say what should be different.")

    position = positions.update(symbol, **changes)
    return f"Updated {position['symbol']}: {', '.join(sorted(changes))}."


@tool(
    name="remove_position",
    description=(
        "Stop tracking a holding, e.g. once the user has sold it. This only removes Ada's "
        "record — it does not sell anything, and Ada has no way to."
    ),
    input_schema={
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "Ticker to stop tracking."}},
        "required": ["symbol"],
    },
)
def remove_position(symbol: str) -> str:
    position = positions.remove(symbol)
    return f"Stopped tracking {position['symbol']}. Nothing was sold — Ada can't."


@tool(
    name="check_price",
    description=(
        "Look up the current price of any ticker, with context: today's move, where it sits "
        "against its 52-week high and its 50-day average, and RSI. Works for anything, not "
        "just what the user holds.\n\n"
        "Present the indicators as context, never as a recommendation. RSI being high is not "
        "a reason to sell and you should not say it is — if the user asks whether to buy or "
        "sell, say plainly that you don't know and can't tell them."
    ),
    input_schema={
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "Ticker, e.g. 'AAPL' or 'BTC-USD'."}},
        "required": ["symbol"],
    },
)
def check_price(symbol: str) -> str:
    try:
        return market.describe(market.read(symbol))
    except market.MarketError as e:
        raise ValueError(f"Couldn't get a price for {symbol.upper()}: {e}") from e


@tool(
    name="review_holding",
    description=(
        "Run a full two-sided review of a stock: one researcher builds the strongest "
        "honest case for holding it, another independently builds the case against, and "
        "a third weighs both against what the user themselves wrote down when they "
        "bought it.\n\n"
        "Use this when the user asks you to review, re-check, stress-test or think hard "
        "about a holding — or asks whether their reason for owning something still "
        "holds. Do NOT use it for a simple price question; check_price is instant and "
        "free, and this is neither.\n\n"
        "It takes a minute or two and costs roughly $0.50 in model usage — three turns "
        "with real web research. Say so before running it: at that price it is for a "
        "position the user is genuinely uneasy about, not a routine check.\n\n"
        "It deliberately does not tell anyone whether to buy or sell, and neither should "
        "you when you report it. What it produces is the two cases, the conditions each "
        "needs, and whether the user's own invalidation condition has moved. Present it "
        "that way."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker to review, e.g. 'NVDA'."},
            "full": {
                "type": "boolean",
                "description": (
                    "Include both raw arguments as well as the briefing. Defaults to "
                    "false — the briefing is the point, the arguments are the workings."
                ),
            },
        },
        "required": ["symbol"],
    },
)
def review_holding(symbol: str, full: bool = False) -> str:
    from ada import debate

    result = debate.review(symbol)
    parts = [result["briefing"]]

    if full:
        parts.append(f"\n--- the case for ---\n{result['bull']}")
        parts.append(f"\n--- the case against ---\n{result['bear']}")

    parts.append(
        f"\n(That review cost {result['spent']:.4f} USD. It is not advice, and running it "
        "again may word things differently — treat the conditions as the output, not any "
        "single sentence.)"
    )
    return "\n".join(parts)
