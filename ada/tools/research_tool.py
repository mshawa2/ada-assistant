"""Market research: a cheap sweep, a full team, and the outside framework.

Three tools at three prices, and the descriptions say which is which — the
failure mode otherwise is a model reaching for the dollar one to answer "what's
NVDA at", which `check_price` does instantly and free.

None of these can trade. Ada has no order path at all; every one of them
returns text.
"""

from datetime import date

from ada import analyst, tradingagents
from ada.tools.registry import tool


@tool(
    name="market_scan",
    description=(
        "Read what's actually moving in the market right now and come back with three to "
        "five candidate names worth a closer look, each with the specific dated thing that "
        "happened and the obvious reason it might not matter.\n\n"
        "Use this when the user asks what's happening in the market, what's worth looking "
        "at, or for ideas. Takes about a minute and costs roughly $0.20 in model usage.\n\n"
        "It produces a SHORTLIST FOR FURTHER WORK, not recommendations — present it that "
        "way. If the user wants a real opinion on one of them, that's analyze_ticker."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def market_scan() -> str:
    return (
        analyst.scan()
        + "\n\n(A scan of public news at one moment — a shortlist to investigate, not "
        "recommendations. analyze_ticker will do the real work on any of these.)"
    )


@tool(
    name="analyze_ticker",
    description=(
        "Put a full analyst team on one ticker: news, fundamentals, sentiment and technical "
        "analysts each research it independently, a bull and a bear argue it out, a trader "
        "makes a call with a conviction level, and a risk manager checks that call against "
        "the user's ACTUAL holdings and can downgrade or veto it.\n\n"
        "Use this when the user wants a real opinion on whether to buy or sell something, or "
        "asks you to properly dig into a name.\n\n"
        "It takes several minutes and costs roughly $1.00 in model usage — say so before "
        "running it. Do NOT use it for a price check; check_price is instant and free.\n\n"
        "Report the trader's call AND the risk manager's verdict — the risk verdict is the "
        "operative one, since it's the only part that knows what they already hold. Ada "
        "cannot place orders; never imply anything was bought or sold."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker to analyse, e.g. 'NVDA'."},
            "full": {
                "type": "boolean",
                "description": "Include the four analyst reports as well as the call. Defaults to false.",
            },
        },
        "required": ["symbol"],
    },
)
def analyze_ticker(symbol: str, full: bool = False) -> str:
    result = analyst.analyze(symbol)

    parts = [f"=== THE TRADER'S CALL ON {result['symbol']} ===", result["call"], "", "=== RISK ===", result["risk"]]
    if full:
        for name, text in result["reports"].items():
            parts += ["", f"=== {name.upper()} ===", text]
        parts += ["", "=== THE CASE FOR ===", result["bull"], "", "=== THE CASE AGAINST ===", result["bear"]]

    parts.append(
        "\n(One run of a model over public information. Run it twice if it matters — the "
        "wording, and sometimes the call, will differ. Ada cannot trade.)"
    )
    return "\n".join(parts)


@tool(
    name="tradingagents_report",
    description=(
        "Run the external TradingAgents framework on a ticker — a separate open-source "
        "multi-agent trading research system, in its own environment.\n\n"
        "Only use this when the user asks for TradingAgents specifically, or wants a second "
        "opinion to compare against analyze_ticker. It is slower, its spending is NOT "
        "counted against Ada's budget because it runs as a separate process, and it doesn't "
        "know what the user holds. analyze_ticker is the better default.\n\n"
        "If it isn't installed, pass on the setup instructions rather than apologising — "
        "it's one script."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker to analyse."},
            "date": {
                "type": "string",
                "description": "Date to analyse as of, YYYY-MM-DD. Defaults to today.",
            },
            "depth": {
                "type": "integer",
                "description": "Debate rounds. 1 is quick, 3 is thorough and much slower. Defaults to 1.",
            },
        },
        "required": ["symbol"],
    },
)
def tradingagents_report(symbol: str, date_: str = "", depth: int = 1, **kwargs) -> str:
    as_of = kwargs.get("date") or date_ or date.today().isoformat()

    try:
        result = tradingagents.run(symbol, as_of, depth=max(1, min(depth or 1, 3)))
    except tradingagents.NotInstalled as e:
        return str(e)

    if not result.get("ok"):
        return f"TradingAgents couldn't finish: {result.get('error', 'unknown error')}"

    parts = [f"TradingAgents on {symbol.upper()} as of {as_of}:", "", result["decision"]]
    for name, text in (result.get("reports") or {}).items():
        parts += ["", f"--- {name} ---", text[:1500]]
    parts.append(
        "\n(From the external TradingAgents framework. Its own authors say it isn't advice, "
        "and its issue tracker has reports of the same ticker giving contradictory calls "
        "between runs — treat one run as a sample. It doesn't know your holdings.)"
    )
    return "\n".join(parts)
