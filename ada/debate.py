"""Arguing both sides of a holding, properly, before you decide anything.

This is the one idea worth taking from TradingAgents (Apache-2.0): decompose a
judgement into roles that argue, then synthesize. What isn't worth taking is
its plumbing — LangGraph, four provider SDKs, pandas, redis and a backtesting
engine, to do something Ada's own agent core already does. She has per-role
tool allowlists, a budget meter, a scheduler and a job table; the framework
was the easy half.

## Why separate contexts, not one clever prompt

Asking one model to "argue both sides" in a single turn produces a bull case
and then a bear case *written by something that already made up its mind* —
the second half is anchored on the first. Each side here gets its own message
list and does its own research, and neither sees the other until the synthesis
reads both. That independence is the whole mechanism; without it this is a
long prompt pretending to be a panel.

## What it will not do

**It never says buy or sell.** The output is the two cases, what would have to
be true for each, and — the part that matters — which of the *user's own*
written invalidation conditions have moved. A model that has read a week of
headlines is not better than you at deciding whether to sell your position; it
is much better than you at noticing that the thing you said would make you
wrong has quietly started happening.

Non-determinism is real and stated: run this twice and the wording differs.
That's why it produces cases and conditions rather than a verdict — a verdict
that changes between runs is noise wearing a suit.

## Cost

**About $0.50 a review, measured.** Three multi-round turns, and each side
spends its full round budget on search and fetch — the first live run made 26
tool calls between the two researchers. Against a $5/day cap that's ten
reviews, so this is rate-limited hard, it reports what it spent every time, and
anything scheduling it should give the job a `budget_usd` slice.

That price is also the argument for what it isn't: at fifty cents a run this
can't be a daily sweep of a watchlist. It's for the position you're actually
uneasy about.
"""

from ada import cost, positions

# Read-only research, and deliberately nothing gated. A debate about whether to
# hold a stock has no business being able to email anyone, and the allowlist in
# `agent.run_turn` is a hard boundary rather than a hint — see AGENT.md.
RESEARCH_TOOLS = [
    "check_price",
    "web_search",
    "fetch_url",
    "list_notes",
    "read_note",
    "search_notes",
]

MAX_ROUNDS = 6

_BULL = """You are building the strongest honest case FOR continuing to hold {symbol}.

Research it: check the current price and context, and search for recent news, \
earnings commentary and anything material from the last few weeks. Cite what you \
actually found — URLs where you have them.

Argue the bull case as well as it can honestly be argued. Do not overstate, do not \
invent, and if the evidence is thin say so plainly — a weak case stated weakly is \
useful, a weak case dressed up is worse than nothing.

End with: the two or three things that would have to REMAIN TRUE for this case to \
hold."""

_BEAR = """You are building the strongest honest case AGAINST continuing to hold {symbol}.

Research it: check the current price and context, and search for recent news, \
earnings commentary, competitive or regulatory developments, and anything material \
from the last few weeks. Cite what you actually found — URLs where you have them.

Argue the bear case as well as it can honestly be argued. Do not overstate, do not \
invent, and if the evidence is thin say so plainly.

End with: the two or three things that would have to BECOME TRUE for this case to \
play out."""

_SYNTHESIS = """Two independent researchers argued opposite sides of {symbol}. \
Neither saw the other's work.

--- THE CASE FOR HOLDING ---
{bull}

--- THE CASE AGAINST HOLDING ---
{bear}

--- WHAT THE HOLDER WROTE DOWN, BEFORE ANY OF THIS ---
{position}

Write a short briefing for the holder. In this order:

1. **Where the two sides actually disagree.** Not a summary of each — the specific \
factual question they answer differently. If they mostly agree, say that; it's the \
more useful finding.
2. **What each side needs to be true.** Plainly, as conditions that can be checked \
later.
3. **Their own invalidation condition.** This is the important part. They wrote down \
what would make them wrong. Say whether anything in either case moves it closer to \
being met, further away, or leaves it untouched — and quote their words back to them. \
If they never wrote one down, say so and say it's the gap worth closing.

Do NOT recommend buying, selling or holding. Do not give a rating, a score, a price \
target or a probability. You are not better than they are at that decision, and \
pretending otherwise is the failure mode this whole exercise exists to avoid. Give \
them the cases and the conditions; the decision is theirs.

Be concise. They will read this on a phone."""


def _no_confirmations(action):
    """A gate that should never be reached.

    The allowlist above contains nothing gated, so a confirmation here means
    the list and the config have drifted apart. Failing loudly is right:
    silently approving would let a research turn take an action nobody asked
    for, and silently declining would hide the drift.
    """
    raise RuntimeError(
        f"A research turn tried to run '{action.tool}', which needs confirmation. "
        "Nothing in debate.RESEARCH_TOOLS should be gated — the allowlist and the "
        "autonomy config have drifted."
    )


def run_role(prompt: str, on_status, tools: list[str]) -> str:
    """One role's whole turn, in its own context.

    `tools` is empty for the synthesis: it has both cases in front of it and
    nothing left to look up, and a turn that can search will search — spending
    money to fetch a headline it has already been handed.
    """
    # Imported here, not at module scope, for the reason heartbeat/executor.py
    # already documents: `ada.agent` imports the tool registry, and the registry
    # imports the tools that import this file. Importing `ada.tools` before
    # `ada.agent` would otherwise fail on a half-built module — which it did,
    # from a test that touched the tools first.
    from ada import agent

    messages = [{"role": "user", "content": prompt}]
    chunks: list[str] = []
    agent.run_turn(
        messages,
        on_text=chunks.append,
        on_status=on_status,
        confirm=_no_confirmations,
        allowed_tools=tools,
    )
    return _final_text(messages) or "".join(chunks).strip()


def _final_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text = "".join(
            (b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or ""
            for b in content
            if (b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "text"
        ).strip()
        if text:
            return text
    return ""


def _position_note(symbol: str) -> str:
    """What the holder already committed to, in their own words.

    The whole briefing turns on this. Without it the synthesis is a market
    opinion, which is worth very little; with it, it's a check against a
    promise they made themselves.
    """
    held = positions.get(symbol)
    if not held:
        return (
            f"They don't currently hold {symbol} — this is research on something they "
            "don't own, so there is no prior thesis to check against."
        )

    lines = [f"They hold {held['shares']:g} shares."]
    if held.get("cost"):
        lines.append(f"Average cost {held['cost']:.2f}.")
    if held.get("stop"):
        lines.append(f"Stop they set: {held['stop']:.2f}.")
    if held.get("target"):
        lines.append(f"Target they set: {held['target']:.2f}.")
    if held.get("thesis"):
        lines.append(f'Why they bought it, in their words: "{held["thesis"]}"')
    if held.get("invalidation"):
        lines.append(f'What they said would make them wrong: "{held["invalidation"]}"')
    else:
        lines.append("They never wrote down what would make them wrong.")
    return " ".join(lines)


def review(symbol: str, on_status=lambda _: None) -> dict:
    """Run both sides and the synthesis. Returns the parts and what it cost."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ValueError("Which holding should be reviewed?")

    with cost.meter() as spent:
        on_status(f"researching the case for {symbol}")
        bull = run_role(_BULL.format(symbol=symbol), on_status, RESEARCH_TOOLS)

        on_status(f"researching the case against {symbol}")
        bear = run_role(_BEAR.format(symbol=symbol), on_status, RESEARCH_TOOLS)

        on_status("weighing both against what you wrote down")
        briefing = run_role(
            _SYNTHESIS.format(
                symbol=symbol, bull=bull, bear=bear, position=_position_note(symbol)
            ),
            on_status,
            [],
        )
        billed = spent()

    return {"symbol": symbol, "bull": bull, "bear": bear, "briefing": briefing, "spent": billed}
