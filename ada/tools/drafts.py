import re
import time

from ada import store
from ada.tools.registry import tool

_DRAFTS_DIR = store.DATA_DIR / "drafts"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "draft"


@tool(
    name="save_draft",
    description=(
        "Save a drafted message (email, text, or reply) for the user to review and send "
        "themselves. This does NOT send anything anywhere — Ada has no ability to send "
        "messages. Use this whenever the user asks you to draft, write, or compose a "
        "message for them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "recipient": {"type": "string", "description": "Who the message is for, e.g. 'Mom' or 'the landlord'."},
            "subject": {"type": "string", "description": "Optional subject line, for emails. Omit for texts/casual messages."},
            "body": {"type": "string", "description": "The full text of the drafted message."},
        },
        "required": ["recipient", "body"],
    },
)
def save_draft(recipient: str, body: str, subject: str = "") -> str:
    filename = f"{int(time.time())}-{_slug(recipient)}.txt"
    header = f"To: {recipient}\n" + (f"Subject: {subject}\n" if subject else "")
    store.write_text(_DRAFTS_DIR / filename, f"{header}\n{body}\n")
    return f"Saved draft to {recipient} as '{filename}'. Not sent — the user needs to review and send it themselves."


@tool(
    name="list_drafts",
    description="List the user's saved message drafts.",
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_drafts() -> str:
    _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(p.name for p in _DRAFTS_DIR.iterdir() if p.is_file())
    return "\n".join(files) if files else "No drafts yet."


@tool(
    name="read_draft",
    description="Read back the full contents of a saved draft by filename, as returned by list_drafts.",
    input_schema={
        "type": "object",
        "properties": {"filename": {"type": "string", "description": "The draft's filename."}},
        "required": ["filename"],
    },
)
def read_draft(filename: str) -> str:
    path = (_DRAFTS_DIR / filename).resolve()
    if _DRAFTS_DIR.resolve() not in path.parents:
        raise ValueError("Invalid draft filename.")
    if not path.exists():
        raise ValueError(f"No draft called '{filename}'.")
    return path.read_text()
