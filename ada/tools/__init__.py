from ada import extensions
from ada.tools import (  # noqa: F401  imported for tool-registration side effects
    approvals_tool,
    brief_tool,
    calendar_tool,
    dev_tools,
    discovery,
    contacts,
    drafts,
    email_tool,
    freetime_tool,
    heartbeat_control,
    home,
    imessage,
    jobs_tool,
    mac,
    market_watch,
    memory,
    messages,
    notes,
    notices,
    people_tools,
    positions_tool,
    portfolio_tool,
    purchasing,
    reminders,
    research_tool,
    schedules_tool,
    search,
    settings_tool,
    shopping,
    weather,
    web,
)
from ada.tools.registry import call, specs

# After the built-ins, so a private module can't shadow one by accident
# without the registry's duplicate check seeing it.
extensions.load()

__all__ = ["call", "specs"]
