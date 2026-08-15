"""Letting Ada read and change her own settings, in conversation.

This is the base spec's fourth rail — "change a setting" — finally having a
tool to gate. Until now the category was theoretical: there was nothing Ada
could change, so the confirmation gate only ever covered deletions.

`set_setting` is gated in `ada_config.json`. `set_mode` deliberately isn't,
and that's a judgement call worth stating: a mode only ever applies a preset
the user wrote themselves, it's bounded, and it's instantly reversible.
Gating it would mean typing y every time you said "focus mode", which is the
kind of friction that makes people switch a safety feature off entirely. It's
listed explicitly under `autonomy.tools` so it reads as a decision rather than
an oversight — and it's one word away from being gated if you disagree.
"""

import json

from ada import settings
from ada.tools.registry import tool

# Changing these through conversation would be a foot-gun: the model would be
# editing the rules that govern the model. Hand-edit ada_config.json instead —
# a deliberate speed bump on the one setting that decides what needs asking.
_PROTECTED_PREFIXES = ("autonomy",)


@tool(
    name="get_settings",
    description=(
        "Show the user's current settings — identity, locale, model, autonomy levels, "
        "notifications, quiet hours, heartbeat checks and modes. Use this when the user "
        "asks how something is configured, what mode they're in, or what you're allowed "
        "to do."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": (
                    "Optional top-level section to show on its own, e.g. 'notifications' "
                    "or 'locale'. Omit to show everything."
                ),
            }
        },
        "required": [],
    },
)
def get_settings(section: str = "") -> str:
    config = settings.load()
    if section:
        if section not in config:
            raise ValueError(f"No settings section called '{section}'. Try one of: {', '.join(config)}.")
        config = {section: config[section]}
    return json.dumps(config, indent=2)


@tool(
    name="set_setting",
    description=(
        "Change one setting, by dotted path. Use this when the user asks to change how "
        "you work — their name, timezone, tone, notification channels, quiet hours, a "
        "heartbeat check's interval. Example paths: 'identity.user_name', "
        "'locale.timezone', 'locale.time_format', 'notifications.ntfy_topic', "
        "'quiet_hours.start', 'heartbeat.checks.due_reminders.enabled'. "
        "Call get_settings first if you're unsure of the exact path. This asks the user "
        "to confirm before it takes effect."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Dotted path to the setting, e.g. 'locale.timezone'.",
            },
            "value": {
                "type": "string",
                "description": (
                    "The new value, as JSON. Strings need quotes ('\"Europe/London\"'); "
                    "use true/false for toggles, a bare number for numbers, and a JSON "
                    "array for lists like notification channels."
                ),
            },
        },
        "required": ["path", "value"],
    },
)
def set_setting(path: str, value: str) -> str:
    if path.split(".")[0] in _PROTECTED_PREFIXES:
        raise ValueError(
            f"'{path}' controls what you're allowed to do without asking, so it can't be "
            "changed from a conversation. The user can edit ada_config.json directly."
        )

    # Checked before the write: a path that doesn't exist yet is legitimate,
    # but it's far more often a typo, and silently creating a setting nothing
    # reads is exactly the kind of thing you'd never notice.
    is_new = settings.get(path) is None

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        # A bare word is what a model reaches for most often; treat it as the
        # string it obviously means rather than failing on a missing quote.
        parsed = value

    settings.set_path(path, parsed)

    note = " (that path was new — worth checking it isn't a typo)" if is_new else ""
    return f"Set {path} to {json.dumps(parsed)}.{note}"


@tool(
    name="set_mode",
    description=(
        "Switch the user's active mode, which changes several settings at once — how "
        "loudly you notify them, which background checks run, quiet hours. Use this when "
        "they say things like 'focus mode', 'I'm off for the weekend', or 'back to "
        "normal'. Call list_modes first if you don't know what's available."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "description": "The mode to switch to, or 'default' to go back to normal.",
            }
        },
        "required": ["mode"],
    },
)
def set_mode(mode: str) -> str:
    presets = settings.get("modes.presets", {}) or {}
    if mode != "default" and mode not in presets:
        available = ", ".join(["default", *presets])
        raise ValueError(f"No mode called '{mode}'. Available: {available}.")

    settings.set_path("modes.active", mode)
    if mode == "default":
        return "Back to normal settings."
    changed = ", ".join(presets[mode]) or "nothing"
    return f"Switched to '{mode}' mode (changes: {changed})."


@tool(
    name="list_modes",
    description=(
        "List the user's available modes and what each one changes, plus which is "
        "currently active."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_modes() -> str:
    active = settings.get("modes.active", "default")
    presets = settings.get("modes.presets", {}) or {}

    lines = [f"default{' (active)' if active == 'default' else ''} — your normal settings"]
    for name, preset in presets.items():
        marker = " (active)" if name == active else ""
        lines.append(f"{name}{marker} — changes: {json.dumps(preset)}")
    return "\n".join(lines)
