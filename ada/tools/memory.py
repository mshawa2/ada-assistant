from ada import store
from ada.tools.registry import tool

_STORE = store.DATA_DIR / "memory.json"

# Shared with every other reader/writer of this file. The read-modify-write
# pairs below run under it because `build_system_prompt()` reads this store on
# every single model call — including from the heartbeat thread — so an
# unlocked rewrite here is a live race, not a theoretical one.
_lock = store.lock_for(_STORE)


def load_facts() -> list[dict]:
    with _lock:
        return store.read_json(_STORE, [])


def _save(facts: list[dict]) -> None:
    store.write_json(_STORE, facts)


@tool(
    name="remember",
    description=(
        "Save a durable fact about the user that should persist across conversations — "
        "their name, a preference, a decision, something true about their life. Use this "
        "when the user tells you something worth carrying forward long-term, or explicitly "
        "asks you to remember something. Don't use this for passing chatter that isn't "
        "worth keeping."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "One clear, single-statement fact, e.g. 'Prefers morning meetings.'",
            },
        },
        "required": ["fact"],
    },
)
def remember(fact: str) -> str:
    with _lock:
        facts = load_facts()
        new_id = max((f["id"] for f in facts), default=0) + 1
        facts.append({"id": new_id, "text": fact})
        _save(facts)
    return f"Remembered (#{new_id}): {fact}"


@tool(
    name="list_memory",
    description=(
        "List everything Ada currently remembers about the user long-term. Use this if the "
        "user asks what you know or remember about them."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_memory() -> str:
    facts = load_facts()
    if not facts:
        return "Nothing remembered yet."
    return "\n".join(f"#{f['id']}: {f['text']}" for f in facts)


@tool(
    name="update_memory",
    description=(
        "Correct an existing remembered fact by id, replacing its text. Use this when the "
        "user corrects something you previously remembered."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The id of the fact to update, as shown by list_memory."},
            "fact": {"type": "string", "description": "The corrected fact text."},
        },
        "required": ["id", "fact"],
    },
)
def update_memory(id: int, fact: str) -> str:
    with _lock:
        facts = load_facts()
        for f in facts:
            if f["id"] == id:
                f["text"] = fact
                _save(facts)
                return f"Updated #{id} to: {fact}"
    raise ValueError(f"No remembered fact with id {id}.")


@tool(
    name="forget",
    description=(
        "Delete a remembered fact by id. Use this when the user asks you to forget "
        "something, or it's no longer true."
    ),
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "The id of the fact to forget, as shown by list_memory."}},
        "required": ["id"],
    },
)
def forget(id: int) -> str:
    with _lock:
        facts = load_facts()
        remaining = [f for f in facts if f["id"] != id]
        if len(remaining) == len(facts):
            raise ValueError(f"No remembered fact with id {id}.")
        _save(remaining)
    return f"Forgot #{id}."
