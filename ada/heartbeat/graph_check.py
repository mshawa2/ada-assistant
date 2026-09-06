"""Keeping the knowledge graph current, quietly.

The only heartbeat check here that exists to do maintenance rather than to tell
the user something. It normally says nothing at all: a graph rebuilt on time is
not news, and an hourly notice saying "the graph is fine" would be exactly the
noise every other check is written to avoid.

It speaks in one case — the first rebuild after the graph has drifted a long
way — because a query answered from a stale graph is quietly wrong, and if that
has been happening it is worth knowing it has stopped.

Work is capped at two repositories per pass. Checks run sequentially on one
thread, so a pass that rebuilt everything at once would hold the heartbeat and
delay a reminder due in that window. What is skipped stays stale and is picked
up next time, which is the right trade for maintenance.
"""

import time

from ada import graph, store
from ada.heartbeat.notices import add_notice

CHECK_NAME = "graph"

# Long enough that a burst of commits becomes one rebuild rather than six.
_DEFAULT_EVERY = 3600.0

# Only worth mentioning if the graph has been wrong for most of a day.
_ANNOUNCE_AFTER = 12 * 3600.0


def check_graph(params: dict) -> None:
    state = store.read_json(graph._STATE, {})
    now = time.time()

    if now - (state.get("checked_at") or 0) < params.get("every_seconds", _DEFAULT_EVERY):
        return

    # Stamped before the work, not after. A rebuild that dies mid-parse must not
    # leave the check retrying on every tick.
    state["checked_at"] = now
    store.write_json(graph._STATE, state)

    drifted_for = now - (state.get("merged_at") or now)
    result = graph.rebuild(limit=params.get("repos_per_pass", 2))

    if not (result["rebuilt"] or result["vault"]):
        return

    if drifted_for > params.get("announce_after_seconds", _ANNOUNCE_AFTER):
        changed = ", ".join(result["rebuilt"]) or "the vault"
        add_notice(
            CHECK_NAME,
            f"Knowledge graph refreshed after {drifted_for / 3600:.0f} hours ({changed}). "
            "Answers from it were describing older code until now.",
            level=params.get("level", "normal"),
        )
