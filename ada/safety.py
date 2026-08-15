"""Which tools Ada may run, and which need you to say yes first.

This is the base spec's send/spend/delete/settings rail made concrete. It used
to be a flat list of gated tool names in `safety_config.json`; it's now a
per-tool autonomy level in `ada_config.json`, because a binary gated/ungated
list can't express the two things a send-or-spend tool needs: "always ask"
and "never, not even if asked".

The levels live in `ada/settings.py`. This module is the thin seam the agent
core asks — kept so there's still one obvious place to look for "what is Ada
allowed to do", rather than that answer being spread through the settings
file's other concerns.
"""

from ada import settings


def requires_confirmation(tool_name: str) -> bool:
    """True if this tool must be confirmed in chat before it runs."""
    return settings.autonomy_for(tool_name) == settings.CONFIRM


def is_denied(tool_name: str) -> bool:
    """True if this tool is switched off entirely.

    Distinct from "not registered": the tool exists and the model can see it,
    but the user has decided it may not run. The model is told that plainly so
    it can say so rather than silently retrying.
    """
    return settings.autonomy_for(tool_name) == settings.DENY


def load_confirmation_required_tools() -> set[str]:
    """Every tool currently set to `confirm`."""
    autonomy = settings.get("autonomy", {}) or {}
    tools = autonomy.get("tools", {})
    gated = {name for name, level in tools.items() if level == settings.CONFIRM}
    if autonomy.get("default") == settings.CONFIRM:
        # Everything not named explicitly is gated too — worth surfacing,
        # since the caller is asking "what needs confirming?".
        from ada.tools import specs

        gated |= {spec["name"] for spec in specs() if spec["name"] not in tools}
    return gated
