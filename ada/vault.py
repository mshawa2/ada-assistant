"""The markdown vault: Ada's knowledge, stored the way a person can read it.

`data/*.json` stays where it is. That split is the whole point of this module,
so it's worth stating plainly: a job queue, a schedule and a spend ledger are
*operational state* — written by the heartbeat while nobody is watching, read
back with a schema, and occasionally raced between the REPL thread and the
loop. Markdown would cost those files atomic writes and buy nothing, because
nobody wants to hand-edit a job queue.

Knowledge is the opposite. Notes, decisions, project pages and contacts are
written once and read by a human many times, and their value is in how they
connect. So they live here as markdown with `[[wikilinks]]`, and the vault is
the *source of truth* rather than an export — Ada writes these files, the user
edits the same files in Obsidian, and neither has to sync. There is one copy.

That choice has a consequence worth naming: anything written here can be edited
or deleted out from under Ada by a person with a text editor. Every read has to
tolerate a file that moved, a frontmatter block someone reformatted, and a link
pointing at a page that doesn't exist yet. None of that is corruption; it's a
vault being used.

**Why frontmatter is hand-rolled.** PyYAML isn't in `requirements.txt` and this
needs three types — string, list of strings, and a date. A dependency for that
is a poor trade, and the parser stays forgiving: a line it can't read is
skipped rather than raising, because a half-understood frontmatter block should
still let you read the note underneath it.
"""

import os
import re
from datetime import date
from pathlib import Path

from ada import store

VAULT_DIR = Path(
    os.environ.get("ADA_VAULT_DIR") or Path.home() / "Vault"
).expanduser()

# Ordered: this is also the order `ensure()` creates them and the order a
# briefing walks them, so it reads top-down from "what's happening" to "who".
SECTIONS = ("daily", "projects", "decisions", "notes", "contacts")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def ensure() -> None:
    """Create the vault skeleton. Safe to call on every read."""
    for section in SECTIONS:
        (VAULT_DIR / section).mkdir(parents=True, exist_ok=True)


def slug(title: str) -> str:
    """A filename that survives a round trip through Finder and a URL.

    Obsidian resolves `[[Some Note]]` against the *filename*, so the slug is
    part of the link contract: change how this works and existing links break.
    """
    out = _SLUG_STRIP.sub("-", title.strip().lower()).strip("-")
    return out or "untitled"


def path_for(section: str, name: str) -> Path:
    """Resolve `section/name` inside the vault, refusing to escape it.

    The traversal check is the same shape as `notes.py`'s, and for the same
    reason: a filename can arrive from a model, and `../../.ssh/id_rsa` is a
    string like any other.
    """
    if section not in SECTIONS:
        raise ValueError(f"unknown vault section: {section}")
    stem = name[:-3] if name.endswith(".md") else name
    path = (VAULT_DIR / section / f"{slug(stem)}.md").resolve()
    if (VAULT_DIR / section).resolve() not in path.parents:
        raise ValueError(f"path escapes the vault: {name}")
    return path


def render_frontmatter(fields: dict) -> str:
    """Serialise the three types this vault actually uses.

    Values are emitted unquoted where that's unambiguous, because the vault is
    read by humans far more often than by this parser.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a page into (fields, body).

    A page with no frontmatter is not an error — a note the user typed by hand
    in Obsidian won't have one, and it's still a note.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    fields: dict = {}
    for line in text[4:end].splitlines():
        key, sep, raw = line.partition(":")
        if not sep:
            continue  # not a field; skip rather than raise
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            fields[key.strip()] = [v.strip() for v in raw[1:-1].split(",") if v.strip()]
        else:
            fields[key.strip()] = raw
    body = text[end + 4 :]
    return fields, body.lstrip("\n")


def write_page(
    section: str,
    title: str,
    body: str,
    *,
    tags: list[str] | None = None,
    links: list[str] | None = None,
    **extra,
) -> Path:
    """Write a page, replacing any existing one at the same slug.

    `links` become a `Related` footer of real `[[wikilinks]]` rather than a
    frontmatter field, because that is what Obsidian's graph and graphify's
    `EXTRACTED` edges both read. A link to a page that doesn't exist yet is
    fine and deliberate — it marks something worth writing later.
    """
    ensure()
    path = path_for(section, title)
    fields = {"title": title, "updated": date.today().isoformat()}
    if tags:
        fields["tags"] = tags
    fields.update(extra)

    parts = [render_frontmatter(fields), "", f"# {title}", "", body.strip()]
    if links:
        parts += ["", "## Related", ""] + [f"- [[{link}]]" for link in links]
    store.write_text(path, "\n".join(parts).rstrip() + "\n")
    return path


def read_page(section: str, name: str) -> str | None:
    """Raw text of a page, or None if it isn't there."""
    path = path_for(section, name)
    return path.read_text() if path.exists() else None


def list_pages(section: str) -> list[str]:
    ensure()
    return sorted(p.stem for p in (VAULT_DIR / section).iterdir() if p.suffix == ".md")


def links_in(text: str) -> list[str]:
    """Every `[[target]]` in a page, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for match in _WIKILINK.findall(text):
        seen.setdefault(slug(match.strip()), None)
    return list(seen)


def backlinks(name: str) -> list[str]:
    """Pages that link *to* `name`, as `section/slug` strings.

    Obsidian computes this itself; Ada needs it to answer "what depends on
    this decision" without opening the app.
    """
    target = slug(name[:-3] if name.endswith(".md") else name)
    found = []
    ensure()
    for section in SECTIONS:
        for path in sorted((VAULT_DIR / section).iterdir()):
            if path.suffix != ".md" or path.stem == target:
                continue
            try:
                if target in links_in(path.read_text()):
                    found.append(f"{section}/{path.stem}")
            except OSError:
                continue  # a file being edited out from under us is not fatal
    return found


def search(query: str, limit: int = 20) -> list[tuple[str, str]]:
    """Case-insensitive substring search. Returns (section/slug, first hit line)."""
    ensure()
    needle = query.lower()
    hits: list[tuple[str, str]] = []
    for section in SECTIONS:
        for path in sorted((VAULT_DIR / section).iterdir()):
            if path.suffix != ".md":
                continue
            try:
                for line in path.read_text().splitlines():
                    if needle in line.lower():
                        hits.append((f"{section}/{path.stem}", line.strip()))
                        break
            except OSError:
                continue
            if len(hits) >= limit:
                return hits
    return hits
