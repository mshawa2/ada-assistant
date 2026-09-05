from ada import vault
from ada.tools.registry import tool

# Notes are knowledge, so they live in the vault the user opens in Obsidian
# rather than under data/, which is for operational state. See ada/vault.py for
# why that line is drawn where it is.
_NOTES_DIR = vault.VAULT_DIR / "notes"


def _safe_note_path(filename: str) -> Path:
    path = (_NOTES_DIR / filename).resolve()
    if _NOTES_DIR.resolve() not in path.parents:
        raise ValueError("Invalid note filename.")
    return path


@tool(
    name="list_notes",
    description="List the filenames of the user's notes. Use this to see what notes exist before reading or searching one.",
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_notes() -> str:
    _NOTES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(p.name for p in _NOTES_DIR.iterdir() if p.is_file())
    return "\n".join(files) if files else "No notes yet."


@tool(
    name="read_note",
    description="Read the full contents of one of the user's notes by filename, as returned by list_notes.",
    input_schema={
        "type": "object",
        "properties": {"filename": {"type": "string", "description": "The note's filename, e.g. 'ideas.md'."}},
        "required": ["filename"],
    },
)
def read_note(filename: str) -> str:
    path = _safe_note_path(filename)
    if not path.exists():
        raise ValueError(f"No note called '{filename}'.")
    return path.read_text()


@tool(
    name="delete_note",
    description=(
        "Permanently delete one of the user's notes by filename. This is irreversible — the "
        "user must explicitly confirm before this runs."
    ),
    input_schema={
        "type": "object",
        "properties": {"filename": {"type": "string", "description": "The note's filename, as returned by list_notes."}},
        "required": ["filename"],
    },
)
def delete_note(filename: str) -> str:
    path = _safe_note_path(filename)
    if not path.exists():
        raise ValueError(f"No note called '{filename}'.")
    path.unlink()
    return f"Deleted note '{filename}'."


@tool(
    name="search_notes",
    description=(
        "Search across all of the user's notes for a keyword or phrase, returning matching "
        "lines with their filenames. Use this to answer questions about what's in the user's "
        "notes without reading every file."
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The word or phrase to search for."}},
        "required": ["query"],
    },
)
def search_notes(query: str) -> str:
    _NOTES_DIR.mkdir(parents=True, exist_ok=True)
    matches = []
    for path in sorted(_NOTES_DIR.iterdir()):
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if query.lower() in line.lower():
                matches.append(f"{path.name}:{lineno}: {line.strip()}")
    return "\n".join(matches) if matches else f"No matches for '{query}'."
