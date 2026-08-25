"""Running the real TradingAgents, without letting it into Ada's process.

TradingAgents (Apache-2.0) is a multi-agent trading research framework. It
installs cleanly on Python 3.14 — verified, not assumed — and pulls **111
packages**: langgraph, four LangChain provider SDKs, pandas, numpy, openai,
google-genai, redis, and backtrader. Ada's own tree is 41.

So it runs in **its own virtualenv, as a subprocess.** Ada calls it and reads
its report back. That's not a compromise, it's the right shape for a dependency
this size: nothing here can break `import ada`, an upgrade on either side can't
disturb the other, and the day it stops being useful the whole thing is one
directory to delete.

## What it is and isn't good for

It's a research-report generator. It has no backtest, no execution, and its
authors say plainly it isn't advice. Its own issue tracker has open reports of
the same ticker producing contradictory calls between runs, so a verdict from
one run is a sample, not a conclusion — the sensible use is reading its
*reasoning*, and running it twice when the answer matters.

It's also slow and not cheap: it drives a dozen-odd model calls of its own,
against whichever provider key it finds, and that spending is **outside Ada's
budget ledger** because it happens in another process. `ada/analyst.py` is the
native path — faster, about a dollar, counted against the cap, and it knows the
user's actual account, which this cannot.
"""

import json
import os
import subprocess
from pathlib import Path

# Installed by `scripts/install_tradingagents.sh` into a sibling of the repo,
# deliberately outside `ada/` so nothing here is importable by accident.
HOME = Path(os.environ.get("TRADINGAGENTS_HOME") or Path.home() / ".ada" / "tradingagents")
VENV_PYTHON = HOME / "venv" / "bin" / "python"

# It thinks for a long time. Past this something has hung rather than run long.
TIMEOUT_SECONDS = 900


class NotInstalled(Exception):
    """TradingAgents isn't set up. A state, not a failure."""


def installed() -> bool:
    return VENV_PYTHON.exists()


def setup_help() -> str:
    return (
        "TradingAgents isn't installed. It's a separate multi-agent research framework "
        "(Apache-2.0) that runs in its own virtualenv — about 111 packages, kept out of "
        "Ada's so it can't break her.\n\n"
        "    ./scripts/install_tradingagents.sh\n\n"
        "It needs its own model provider key in the environment, and its spending is not "
        "counted against Ada's budget — it runs in a separate process. Ada's own analyst "
        "team (analyze_ticker) is faster, cheaper, counted, and knows your actual account."
    )


# Run inside the other venv. Kept as a string rather than a file so there's one
# place to read what Ada asks of it, and nothing to drift out of sync.
_RUNNER = """
import json, sys
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

symbol, date, depth = sys.argv[1], sys.argv[2], int(sys.argv[3])
config = DEFAULT_CONFIG.copy()
config["max_debate_rounds"] = depth
try:
    graph = TradingAgentsGraph(debug=False, config=config)
    state, decision = graph.propagate(symbol, date)
    reports = {k: v for k, v in (state or {}).items() if isinstance(v, str) and len(v) > 40}
    print("---ADA-JSON---")
    print(json.dumps({"ok": True, "decision": str(decision), "reports": reports}))
except Exception as e:
    print("---ADA-JSON---")
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
"""


def run(symbol: str, date: str, depth: int = 1) -> dict:
    """Drive TradingAgents in its own venv and bring the report back.

    Failures come back as a result rather than an exception where they're the
    framework's own — a provider key it doesn't have, a symbol it can't price —
    because those are things to tell the user, not stack traces.
    """
    if not installed():
        raise NotInstalled(setup_help())

    try:
        finished = subprocess.run(
            [str(VENV_PYTHON), "-c", _RUNNER, symbol.upper(), date, str(depth)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=str(HOME),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"TradingAgents didn't finish within {TIMEOUT_SECONDS // 60} minutes."}

    marker = "---ADA-JSON---"
    if marker not in finished.stdout:
        # It writes a great deal to stdout on its way through; the tail is the
        # part that says what went wrong.
        tail = (finished.stderr or finished.stdout or "").strip()[-400:]
        return {"ok": False, "error": f"TradingAgents didn't return a result. {tail}"}

    try:
        return json.loads(finished.stdout.rsplit(marker, 1)[1].strip())
    except json.JSONDecodeError:
        return {"ok": False, "error": "TradingAgents returned something unreadable."}
