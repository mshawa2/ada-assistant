"""Reads (never sends or replies to) recent iMessage/text messages from the
local Messages app database. Opened strictly read-only — this tool can't
write to it, so it can't corrupt or lose anything in Messages.

Needs macOS Full Disk Access granted to whatever terminal app runs Ada
(System Settings > Privacy & Security > Full Disk Access), since
~/Library/Messages/chat.db is otherwise off-limits to sandboxed processes.
"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from ada.tools.registry import tool

_CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
_APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01 (Apple's Cocoa epoch)


def _apple_timestamp_to_str(value: int) -> str:
    seconds = value / 1_000_000_000 if value > 10**12 else value
    return datetime.fromtimestamp(seconds + _APPLE_EPOCH_OFFSET).strftime("%a %I:%M %p")


def _decode_attributed_body(blob: bytes | None) -> str | None:
    """Best-effort text extraction from the legacy NSArchiver 'attributedBody'
    blob Messages uses for rich/reaction messages, when the plain `text`
    column is empty. Not a full archive parser — just enough to usually find
    the readable string. Returns None if it can't."""
    if not blob:
        return None
    try:
        raw = blob.decode("utf-8", errors="ignore")
    except Exception:
        return None
    match = re.search(r"NSString.{0,10}?([\x20-\x7e][^\x00-\x1f]{0,4000})", raw)
    return match.group(1).strip() if match else None


def _connect_readonly() -> sqlite3.Connection:
    if not _CHAT_DB.exists():
        raise ValueError("Can't find the Messages database on this Mac.")
    try:
        return sqlite3.connect(f"file:{_CHAT_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        raise ValueError(
            "Can't read Messages — this needs Full Disk Access. Go to System Settings > "
            "Privacy & Security > Full Disk Access and add your terminal app, then try again."
        ) from e


@tool(
    name="read_recent_messages",
    description=(
        "Read the user's most recent iMessage/text messages on this Mac, newest first, across "
        "all conversations. Shows sender phone numbers/emails, not contact names — Ada has no "
        "access to Contacts. Read-only: Ada cannot send, reply to, or delete any message. Use "
        "this when the user asks you to read, check, or catch them up on their messages/texts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many recent messages to read. Defaults to 10, max 50."},
        },
        "required": [],
    },
)
def read_recent_messages(limit: int = 10) -> str:
    limit = max(1, min(limit, 50))
    conn = _connect_readonly()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT message.date, message.is_from_me, message.text, message.attributedBody,
                   COALESCE(handle.id, 'unknown')
            FROM message
            LEFT JOIN handle ON message.handle_id = handle.ROWID
            ORDER BY message.date DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return "No messages found."

    lines = []
    for date_value, is_from_me, text, attributed_body, sender in rows:
        body = text or _decode_attributed_body(attributed_body) or "[unreadable message]"
        who = "you" if is_from_me else sender
        when = _apple_timestamp_to_str(date_value)
        lines.append(f"[{when}] {who}: {body}")
    return "\n".join(lines)
