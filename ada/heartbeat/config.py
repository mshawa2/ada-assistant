import json
from pathlib import Path

_CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "heartbeat_config.json"

_DEFAULTS = {
    "tick_seconds": 30,
    "quiet_hours": {"start": "22:00", "end": "08:00"},
    "checks": {},
}


def load_config() -> dict:
    if not _CONFIG_FILE.exists():
        return _DEFAULTS
    config = json.loads(_CONFIG_FILE.read_text())
    return {**_DEFAULTS, **config}
