"""Knowing where things stand with a person, and remembering it between times.

`find_contact` answers "what number do I text Jane on". This answers the
question an assistant is actually for: *where are we up to* — when you last
spoke, what about, and whatever you wrote down about them last time.

The page it keeps is co-owned; see `ada/people.py` for why Ada only ever writes
inside a marked block.

**Ambiguity is handed back, not guessed.** `find_contact` refuses to pick
between two people because picking wrong sends a private message to a stranger.
The stakes here are lower — a briefing, not a text — but the failure is the same
shape: silently briefing on the wrong Jane trains the user to trust a page that
is about someone else. So more than one match returns the list and stops.
"""

from ada import people, vault
from ada.tools.contacts import search
from ada.tools.registry import tool


@tool(
    name="contact_brief",
    description=(
        "Where things stand with one person: how to reach them, when you last spoke and "
        "about what, plus anything the user has written on their page in the vault.\n\n"
        "Use before a meeting with someone, or whenever the user asks about their history "
        "with a named person. Refreshes their page as a side effect, so the vault stays "
        "current without anyone maintaining it.\n\n"
        "If more than one contact matches, this returns the list instead of choosing — ask "
        "the user which one they meant. Read-only with respect to messages: Ada cannot send, "
        "reply to or delete anything."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Who to look up, e.g. 'Sam' or 'Jane Doe'."}
        },
        "required": ["name"],
    },
)
def contact_brief(name: str) -> str:
    matches = search(name)

    if not matches:
        # They may still have a page written by hand, with no Contacts entry.
        existing = vault.read_page("contacts", name)
        if existing:
            return f"No contact card for '{name}', but there's a page in the vault:\n\n{existing}"
        return f"No contact matching '{name}', and no page for them in the vault."

    if len(matches) > 1:
        lines = [f"More than one contact matches '{name}' — which did you mean?"]
        for match in matches[:5]:
            handles = ", ".join(h["value"] for h in match["handles"])
            lines.append(f"  {match['name']}: {handles}")
        return "\n".join(lines)

    match = matches[0]
    handles = [h["value"] for h in match["handles"]]
    block = people.refresh(match["name"], handles)

    page = vault.read_page("contacts", match["name"]) or ""
    _, body = vault.parse_frontmatter(page)
    yours = body.replace(people.BEGIN, "").replace(people.END, "")
    yours = yours.replace(block, "").strip()
    # Strip the heading write_page added, so the summary isn't two titles deep.
    if yours.startswith("# "):
        yours = yours.split("\n", 1)[1].strip() if "\n" in yours else ""

    out = [match["name"], "", block]
    if yours:
        out += ["", "## What you've noted", "", yours]
    return "\n".join(out)


@tool(
    name="list_people",
    description=(
        "Everyone the user has a page for in the vault. Use to list tracked people, or to "
        "check whether someone is already tracked before writing about them."
    ),
    input_schema={"type": "object", "properties": {}},
)
def list_people() -> str:
    pages = vault.list_pages("contacts")
    if not pages:
        return "No contact pages yet. Ask about someone with contact_brief and Ada will start one."
    return "\n".join(pages)
