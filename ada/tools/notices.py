from ada.heartbeat.notices import dismiss_notice, list_notices
from ada.tools.registry import tool


@tool(
    name="list_notices",
    description=(
        "List things Ada has proactively noticed and logged from background checks, that "
        "haven't been dismissed yet. Use this when the user asks what you've noticed, "
        "what's new, or what's in the log."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_notices_tool() -> str:
    notices = list_notices()
    if not notices:
        return "Nothing noticed right now."
    return "\n".join(f"#{n['id']} [{n['level']}] {n['text']}" for n in notices)


@tool(
    name="dismiss_notice",
    description="Dismiss a previously noticed item by id, once the user has seen or handled it.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "The id of the notice to dismiss, as shown by list_notices."}},
        "required": ["id"],
    },
)
def dismiss_notice_tool(id: int) -> str:
    if dismiss_notice(id):
        return f"Dismissed notice #{id}."
    raise ValueError(f"No notice with id {id}.")
