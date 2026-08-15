"""The heartbeat's slice of the settings file.

Kept as its own module so `loop.py`, `greeting.py` and `notify.py` don't each
have to know how the settings file is laid out — they ask for a heartbeat
config and get the same shape they always did. What changed underneath is
where it comes from: `ada_config.json` via `ada/settings.py`, which layers in
defaults and the active mode and reloads when the file changes.

That last part matters here specifically. This used to be read once at
`start()`, so retuning an interval meant restarting Ada. Every call now sees
the current file.
"""

from ada import settings


def load_config() -> dict:
    """The heartbeat's view: tick, quiet hours, notifications, checks."""
    config = settings.load()
    heartbeat = config.get("heartbeat", {})
    return {
        "tick_seconds": heartbeat.get("tick_seconds", 30),
        "checks": heartbeat.get("checks", {}),
        "quiet_hours": config.get("quiet_hours", {}),
        "notifications": config.get("notifications", {}),
    }
