"""The heartbeat's on/off switch.

Was three separate zero-argument tools — pause, resume, status. That's three
slots in the model's tool list spent on one switch, and three descriptions it
has to disambiguate between. One tool with an action reads better to a model
and leaves room for tools that do genuinely different things.
"""

from ada.heartbeat.kill_switch import is_paused, set_paused
from ada.tools.registry import tool

_ACTIONS = ("status", "pause", "resume")


@tool(
    name="heartbeat",
    description=(
        "Check or change the background/proactive checks (the heartbeat). Use "
        "action='pause' when the user wants to stop background checks or go quiet, "
        "'resume' to start them again, and 'status' to report whether they're running. "
        "Pausing doesn't affect conversation — you keep working normally, and anything "
        "that came due while paused runs once on resume rather than being lost."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "What to do: 'status', 'pause' or 'resume'. Defaults to 'status'.",
            }
        },
        "required": [],
    },
)
def heartbeat(action: str = "status") -> str:
    action = (action or "status").strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"'{action}' isn't a heartbeat action. Use one of: {', '.join(_ACTIONS)}.")

    if action == "pause":
        set_paused(True)
        return "Heartbeat paused. Background checks won't run again until resumed."
    if action == "resume":
        set_paused(False)
        return "Heartbeat resumed — anything that came due while paused will run once now."

    return "Paused." if is_paused() else "Running normally."
