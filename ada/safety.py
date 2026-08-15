"""Which tools require explicit user confirmation before running. Configured
in safety_config.json, not hardcoded, so the list can change without a code
edit. This is the base spec's list made concrete: send/spend/delete/settings.
"""

import json
from pathlib import Path

_CONFIG_FILE = Path(__file__).resolve().parent.parent / "safety_config.json"


def load_confirmation_required_tools() -> set[str]:
    if not _CONFIG_FILE.exists():
        return set()
    config = json.loads(_CONFIG_FILE.read_text())
    return set(config.get("confirmation_required_tools", []))


def requires_confirmation(tool_name: str) -> bool:
    return tool_name in load_confirmation_required_tools()
