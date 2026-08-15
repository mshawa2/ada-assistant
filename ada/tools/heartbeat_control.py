from ada.heartbeat.kill_switch import is_paused, set_paused
from ada.tools.registry import tool


@tool(
    name="pause_heartbeat",
    description=(
        "Pause all background/proactive checks (the heartbeat) without shutting anything else "
        "down — conversation keeps working normally. Use this when the user asks to stop "
        "background checks, pause proactivity, or go quiet."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def pause_heartbeat() -> str:
    set_paused(True)
    return "Heartbeat paused. Background checks won't run again until resumed."


@tool(
    name="resume_heartbeat",
    description="Resume background/proactive checks (the heartbeat) after they were paused.",
    input_schema={"type": "object", "properties": {}, "required": []},
)
def resume_heartbeat() -> str:
    set_paused(False)
    return "Heartbeat resumed."


@tool(
    name="heartbeat_status",
    description="Report whether the background heartbeat is currently paused or running.",
    input_schema={"type": "object", "properties": {}, "required": []},
)
def heartbeat_status() -> str:
    return "Paused." if is_paused() else "Running normally."
