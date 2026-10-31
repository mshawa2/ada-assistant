"""Modules that live on this machine and nowhere else.

Anything in `ada/private/` (gitignored) is imported at startup, so an
integration that can't be published — someone else's API, a client's data —
plugs into Ada the same way a built-in does, without the public repo knowing it
exists. A private module registers tools with the usual `@tool` decorator, and
adds heartbeat checks and morning-brief sections here. Its settings go in
`ada_config.local.json` (see `settings.py`), which is gitignored for the same
reason.

With no `ada/private/` at all this is a no-op, which is the state of every
fresh clone.
"""

import importlib
import pkgutil
from collections.abc import Callable
from pathlib import Path

_DIR = Path(__file__).parent / "private"

# Same shapes as `heartbeat.checks.CHECKS` and `brief.SECTIONS`, which merge
# these in.
checks: dict[str, Callable[[dict], None]] = {}
brief_sections: list[tuple[str, Callable[[], list[str]]]] = []

_loaded = False


def load() -> None:
    """Import every private module, once. Safe to call from anywhere.

    Marked loaded *before* importing, because a private module importing
    `ada.tools` would otherwise come straight back here and recurse. A module
    that fails to import is reported and skipped: a broken side integration
    must not stop Ada starting, the same bargain every heartbeat check makes.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _DIR.is_dir():
        return
    for module in pkgutil.iter_modules([str(_DIR)]):
        try:
            importlib.import_module(f"ada.private.{module.name}")
        except Exception as e:
            print(f"\n[extensions: couldn't load ada/private/{module.name}: {e}]", flush=True)
