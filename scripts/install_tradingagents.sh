#!/usr/bin/env bash
# Installs TradingAgents into its own virtualenv, kept away from Ada's.
#
# It pulls ~111 packages (langgraph, four LangChain provider SDKs, pandas,
# numpy, redis, backtrader). Ada's own tree is 41 and stays that way: nothing
# installed here is importable from Ada, which calls it as a subprocess.
#
# To remove it entirely: rm -rf ~/.ada/tradingagents
set -euo pipefail

HOME_DIR="${TRADINGAGENTS_HOME:-$HOME/.ada/tradingagents}"
PYTHON="${PYTHON:-python3}"

echo "Installing TradingAgents into $HOME_DIR"
mkdir -p "$HOME_DIR"

if [ -d "$HOME_DIR/src/.git" ]; then
  git -C "$HOME_DIR/src" pull --ff-only
else
  git clone --depth 1 https://github.com/TauricResearch/TradingAgents.git "$HOME_DIR/src"
fi

"$PYTHON" -m venv "$HOME_DIR/venv"
"$HOME_DIR/venv/bin/pip" install --upgrade pip
"$HOME_DIR/venv/bin/pip" install "$HOME_DIR/src"

cat <<'NOTE'

Done. Two things worth knowing:

  * It needs its own model provider key in the environment (OPENAI_API_KEY,
    ANTHROPIC_API_KEY, or whichever provider you configure it for).
  * Its spending is NOT counted against Ada's budget — it runs in a separate
    process with its own client.

Ada's built-in analyst team (analyze_ticker) is faster, roughly a dollar,
counted against your cap, and knows your actual holdings. This is here for
comparison and for its specific analyst decomposition.
NOTE
