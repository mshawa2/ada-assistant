"""The `daily_brief` tool. All the thinking is in `ada/brief.py`."""

from ada import brief
from ada.tools.registry import tool


@tool(
    name="daily_brief",
    description=(
        "The user's whole day in one call: work blocked waiting on their approval, what's "
        "left on their calendar today, reminders that are overdue or due today, anything "
        "the background heartbeat noticed, and the weather where they are. Use this for "
        "'what's on today', 'catch me up', 'how's my day looking', 'anything I need to "
        "know', 'good morning' — any question about the day as a whole rather than one "
        "corner of it. Prefer it over calling list_events, list_reminders and get_weather "
        "separately: it is free, instant, and cannot miss a section."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def daily_brief() -> str:
    return brief.compose()
