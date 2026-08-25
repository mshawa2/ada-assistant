"""Seeing your real accounts. None of these can move money.

Every one is a read. Ada has no order path at all — `brokerage.py` refuses any
path outside a GET allowlist before it opens a socket — and these descriptions
say so, because the moment after a sync is exactly when someone asks "so can you
just sell it then".
"""

from ada import brokerage, portfolio, positions, when
from ada.tools.registry import tool


@tool(
    name="sync_portfolio",
    description=(
        "Pull the user's real holdings, cash and buying power from their connected "
        "brokerage accounts and merge them into what Ada tracks.\n\n"
        "Use this when they ask to sync, refresh, or check what they actually hold, or "
        "when they mention having bought or sold something.\n\n"
        "It only ever reads — Ada cannot place, cancel or change an order, and you must "
        "not imply otherwise.\n\n"
        "Their own stops, targets and written reasons are never overwritten by a sync. "
        "If it reports new holdings, tell them those won't alert until they set a stop "
        "and say what would make them wrong — that's the part that matters."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def sync_portfolio() -> str:
    try:
        return portfolio.describe(portfolio.sync())
    except brokerage.NotConnected as e:
        return str(e)
    except brokerage.BrokerageError as e:
        # Deliberately not phrased as "you hold nothing".
        return f"Couldn't reach the brokerage, so nothing was changed: {e}"


@tool(
    name="list_accounts",
    description=(
        "Show the user's connected brokerage accounts with cash, buying power and when "
        "each was last read. Use this when they ask about their accounts, cash, or how "
        "much they have available.\n\n"
        "This reads the last sync rather than calling the brokerage — run sync_portfolio "
        "first if they want it fresh."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_accounts() -> str:
    last = portfolio.last_sync()
    accounts = last.get("accounts") or []

    if not accounts:
        if not brokerage.configured():
            return brokerage.setup_help()
        return "No accounts have been synced yet. Run sync_portfolio to read them."

    lines = []
    for account in accounts:
        bits = [f"{account['name']} {account['number']}"]
        if account.get("total"):
            bits.append(f"total {account['total']:,.2f}")
        bits.append(f"cash {account['cash']:,.2f}")
        if account.get("buying_power"):
            bits.append(f"buying power {account['buying_power']:,.2f}")
        bits.append(f"{len(account.get('holdings') or [])} holdings")
        lines.append(" · ".join(bits))

    if last.get("at"):
        lines.append(f"\nAs of {when.stamp(last['at'])}.")
    return "\n".join(lines)


@tool(
    name="connection_status",
    description=(
        "Check whether Ada's read-only brokerage connection is set up and healthy. Use "
        "this when a sync fails, when the user asks if she's connected, or when holdings "
        "look stale — a connection that has quietly gone stale looks exactly like a "
        "portfolio that stopped changing."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def connection_status() -> str:
    if not brokerage.configured():
        return brokerage.setup_help()

    try:
        links = brokerage.connections()
    except brokerage.BrokerageError as e:
        return f"Credentials are configured, but the call failed: {e}"

    if not links:
        return (
            "Connected to SnapTrade, but no brokerage is linked to the account. "
            "Link one in the SnapTrade dashboard and sync again."
        )

    lines = []
    for link in links:
        name = (link.get("brokerage") or {}).get("name") or link.get("name") or "brokerage"
        disabled = link.get("disabled")
        lines.append(f"{name}: {'DISABLED — needs reconnecting' if disabled else 'connected'}")
    return "\n".join(lines)


@tool(
    name="account_activity",
    description=(
        "Show recent activity in a brokerage account — fills, dividends, deposits, "
        "withdrawals. Use this when the user asks what's happened in their account, about "
        "dividends, or to check whether a trade went through.\n\n"
        "This calls the brokerage directly, so it's slower than the other reads. It cannot "
        "place or change anything."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Which account, as listed by list_accounts. Omit for the first one.",
            },
            "start": {"type": "string", "description": "Earliest date, YYYY-MM-DD. Optional."},
        },
        "required": [],
    },
)
def account_activity(account_id: str = "", start: str = "") -> str:
    accounts = (portfolio.last_sync().get("accounts") or [])
    if not account_id:
        if not accounts:
            return "No accounts synced yet. Run sync_portfolio first."
        account_id = accounts[0]["id"]

    try:
        rows = brokerage.activities(account_id, start=start)
    except brokerage.NotConnected as e:
        return str(e)
    except brokerage.BrokerageError as e:
        return f"Couldn't read activity: {e}"

    if not rows:
        return "No activity in that window."

    lines = []
    for row in rows[:25]:
        kind = row.get("type") or "activity"
        symbol = (row.get("symbol") or {}).get("symbol") if isinstance(row.get("symbol"), dict) else row.get("symbol")
        amount = row.get("amount") or row.get("price")
        date = (row.get("trade_date") or row.get("settlement_date") or "")[:10]
        lines.append(" · ".join(str(b) for b in (date, kind, symbol, amount) if b))
    return "\n".join(lines)


@tool(
    name="holdings_needing_rules",
    description=(
        "List holdings that Ada tracks but which have no stop or no written reason they'd "
        "be wrong — meaning they will never alert. Use this after a sync, or when the user "
        "asks what still needs setting up.\n\n"
        "This is worth raising unprompted after a sync brings in new holdings: a position "
        "with no stop is being watched in name only."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def holdings_needing_rules() -> str:
    gaps = []
    for held in positions.load_positions():
        missing = []
        if not held.get("stop"):
            missing.append("no stop")
        if not held.get("invalidation"):
            missing.append("nothing written about what would make them wrong")
        if missing:
            gaps.append(f"{held['symbol']} ({held['shares']:g} shares) — {', and '.join(missing)}")

    if not gaps:
        return "Every holding has a stop and a written invalidation."
    return (
        "These are tracked but inert — they'll be priced and shown, and will never alert:\n"
        + "\n".join(gaps)
    )
