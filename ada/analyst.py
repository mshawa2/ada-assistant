"""A full analyst team on one name, and a cheap sweep for candidates.

This is TradingAgents' architecture — specialist analysts, a bull/bear debate,
a trader who synthesises, a risk manager who can veto — reimplemented over
Ada's own agent core. Same shape, none of the 22 dependencies, and one thing
the original can't do: the risk manager sees the user's **actual** account.

That last part is the whole reason this is worth having. A model reading
headlines is not better than the user at picking stocks. It is much better at
reading forty articles they'd never open, and a risk pass that knows their real
cash, their real positions and the daily spending cap they set is a genuinely
different product from generic advice.

## Two tiers, because the deep one costs a dollar

- `scan()` — one research turn over current market news, producing candidate
  ideas with reasons. Around $0.20.
- `analyze(symbol)` — the full team: four analysts researching in parallel
  contexts, a bull/bear debate, a trader, a risk manager. Seven turns, around
  **$1.00**.

Cheap sweep, expensive deep-dive on demand. Running the full team over a
watchlist would cost more per morning than most small accounts make in a month,
which is itself worth knowing.

## What it will and won't do

It **will** give a direction and a conviction, because that is what was asked
for and hedging it into uselessness would be its own kind of dishonesty.

It **cannot trade**. There is no order path in Ada; the output is text. The
risk manager can downgrade or veto the trader, and its sizing is measured
against the user's real position sizes rather than an imagined portfolio.

Every output carries what it is: a model's reading of public information at one
moment, non-reproducible between runs, and not a substitute for the user's own
judgement about their own money.
"""

from ada import budget, debate, positions

RESEARCH_TOOLS = debate.RESEARCH_TOOLS

# Four specialists, each in its own context so none anchors on another's read.
# Deliberately narrow briefs: an analyst told to "analyse the stock" writes a
# little of everything and nothing usefully.
ANALYSTS = (
    (
        "news",
        """You are the news and macro analyst. For {symbol}, find what has actually happened \
in the last two to four weeks: company announcements, earnings, guidance changes, regulatory \
or legal developments, and the macro backdrop that bears on it (rates, sector flows, supply chain).

Search properly and cite URLs. Report FACTS AND DATES, not opinions. If something is rumour \
or unconfirmed, label it. If the last few weeks were quiet, say so — a quiet tape is a finding.""",
    ),
    (
        "fundamentals",
        """You are the fundamentals analyst. For {symbol}, establish what the business actually \
does and how it is performing: revenue growth and its direction, margins, the most recent \
reported quarter versus expectations, balance sheet health, and how the current valuation \
compares to its own history and to peers.

Search for the real numbers and cite where each came from. **Do not state a figure you did not \
find** — say "couldn't verify" instead. A confidently wrong P/E is worse than an admitted gap.""",
    ),
    (
        "sentiment",
        """You are the sentiment analyst. For {symbol}, establish how it is currently being \
talked about: analyst actions and price-target changes, notable institutional or insider \
moves if reported, and the tone of retail and press coverage.

Be explicit that sentiment is a *description of the crowd*, not evidence about the business. \
Where sentiment and fundamentals point opposite ways, say so plainly — that gap is often the \
most useful thing on the page.""",
    ),
    (
        "technical",
        """You are the technical analyst. Use check_price on {symbol} and report its structure: \
where it sits against its 50-day average and its 52-week range, what RSI says about how \
stretched it is, and the shape of the recent move.

State plainly that this describes where the price has been, not where it is going. Give the \
levels that would matter — where it has found support or run into resistance — as *levels*, \
not predictions.""",
    ),
)

_TRADER = """You are the trader. Four analysts and two researchers worked on {symbol}, \
independently. Their reports:

{reports}

--- THE CASE FOR ---
{bull}

--- THE CASE AGAINST ---
{bear}

Make a call. Be decisive — a trader who won't commit is no use to anyone.

Give:
1. **Direction**: buy, hold, avoid, or sell (sell only if they hold it).
2. **Conviction**: low, medium or high, and say honestly what drives it. Low conviction is a \
legitimate answer; false confidence is not.
3. **Why**, in three sentences. The specific thing that decided it, not a summary of the reports.
4. **What would change your mind** — a concrete, checkable condition.
5. **Where you'd be wrong fastest**: the single strongest argument against your own call.

Where the analysts contradicted each other, say which you believed and why. If the evidence is \
genuinely thin, say the honest thing is to pass — that's a call too."""

_RISK = """You are the risk manager, and you have the last word.

The trader's call on {symbol}:
{call}

The user's ACTUAL situation:
{account}

Your job is not to second-guess the thesis. It is to decide whether acting on it is sensible \
*for this person's account as it actually is*. Consider: concentration against what they already \
hold, whether this correlates with existing positions, what a sensible position size would be, \
and where a stop would go.

You may **downgrade or veto** the trader. Say so plainly if you do, and why.

Give:
1. **Verdict**: proceed / proceed smaller / wait / veto.
2. **Size**: what fraction of the account, and what that means as a rough number of shares if \
you can work it out. Small accounts die from oversizing, not from bad picks.
3. **Stop**: an actual price, and the reason that level.
4. **Invalidation**: the sentence they should write down now, before they buy, describing what \
would make them wrong. This is what Ada will hold them to later.

Be concise. End with one line, exactly: "This is a model's reading of public information, not \
advice — you are deciding with your own money."."""

_SCAN = """Find what is genuinely moving in the market RIGHT NOW.

Search current financial news — the last few days. Look for: notable earnings reactions, \
guidance changes, sector rotations, regulatory or macro developments, and anything with an \
unusual move that has an identifiable reason behind it.

Then give **three to five candidate names worth a closer look**. For each:
- ticker and what it does, in one line
- the specific, dated thing that happened
- why that might matter, and the obvious reason it might not
- what you'd need to check before touching it

Rules: cite URLs. Do not include a name whose catalyst you could not actually find — three \
real ones beat five padded out. This is a shortlist for further work, not a set of \
recommendations, and you should say so.

{holdings}"""


def _account() -> str:
    """The user's real position, in the risk manager's terms.

    Generic advice is worth very little; advice that knows what someone already
    holds and what they've capped their spending at is a different thing.
    """
    held = positions.load_positions()
    lines = []

    if held:
        lines.append("They currently hold:")
        for position in held:
            parts = [f"  - {position['shares']:g} {position['symbol']}"]
            if position.get("cost"):
                parts.append(f"at an average cost of {position['cost']:.2f}")
            if position.get("last_price"):
                parts.append(f"worth about {position['last_price'] * position['shares']:.0f}")
            parts.append(
                f"stop {position['stop']:.2f}" if position.get("stop") else "no stop set"
            )
            lines.append(", ".join(parts))
    else:
        lines.append("They hold nothing Ada knows about, so there is no concentration risk yet.")
        lines.append(
            "You do not know their account size. Give sizing as a PERCENTAGE and say plainly "
            "that you don't know the balance — do not invent one."
        )

    caps = [g for g in budget.gauges() if g["category"] == "model"]
    if caps:
        lines.append(
            f"\nFor scale, they cap Ada's own model spending at {caps[0]['cap_text']} per "
            f"{caps[0]['window']} — a sign of how they think about small costs."
        )
    return "\n".join(lines)


def scan(on_status=lambda _: None) -> str:
    """A cheap sweep of current news for candidates. One turn."""
    held = positions.load_positions()
    holdings = (
        f"The user already holds: {', '.join(p['symbol'] for p in held)}. "
        "Don't just re-pitch those — but do flag it if news materially affects one."
        if held
        else ""
    )
    on_status("reading what's moving")
    return debate.run_role(_SCAN.format(holdings=holdings), on_status, RESEARCH_TOOLS)


def analyze(symbol: str, on_status=lambda _: None) -> dict:
    """The full team on one name. Seven turns, around a dollar."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ValueError("Which ticker should the team look at?")

    reports = {}
    for name, brief in ANALYSTS:
        on_status(f"{name} analyst working on {symbol}")
        reports[name] = debate.run_role(brief.format(symbol=symbol), on_status, RESEARCH_TOOLS)

    joined = "\n\n".join(f"--- {name.upper()} ANALYST ---\n{text}" for name, text in reports.items())

    on_status(f"building the case for {symbol}")
    bull = debate.run_role(
        debate._BULL.format(symbol=symbol) + f"\n\nThe analysts found:\n{joined}",
        on_status,
        [],
    )
    on_status(f"building the case against {symbol}")
    bear = debate.run_role(
        debate._BEAR.format(symbol=symbol) + f"\n\nThe analysts found:\n{joined}",
        on_status,
        [],
    )

    on_status("the trader is deciding")
    call = debate.run_role(
        _TRADER.format(symbol=symbol, reports=joined, bull=bull, bear=bear), on_status, []
    )

    on_status("risk is checking it against your account")
    risk = debate.run_role(_RISK.format(symbol=symbol, call=call, account=_account()), on_status, [])

    return {
        "symbol": symbol,
        "reports": reports,
        "bull": bull,
        "bear": bear,
        "call": call,
        "risk": risk,
    }
