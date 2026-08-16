from ada.tools import (  # noqa: F401  imported for tool-registration side effects
    calendar_tool,
    discovery,
    contacts,
    drafts,
    email_tool,
    heartbeat_control,
    home,
    imessage,
    mac,
    memory,
    messages,
    notes,
    notices,
    purchasing,
    reminders,
    search,
    settings_tool,
    shopping,
    weather,
    web,
)
from ada.tools.registry import call, specs

__all__ = ["call", "specs"]
