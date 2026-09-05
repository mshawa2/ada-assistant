"""Telling the user when a build breaks, and when it comes back.

**Only transitions are news.** This is the lesson `sync_check` learned the hard
way: a red build is *permanent state*, not an event. It stays red until someone
fixes it, so a check that notices "CI is failing" on every tick writes the same
sentence into the notice log every few minutes all day — and notices are
deliberately not deduplicated, because other checks repeat on purpose.

So the rule here is a comparison, not an observation. A notice goes out when the
*answer changes*: a run that finished differently from the last one this check
saw. Two transitions are worth a person's attention and nothing else is:

- **green (or unknown) → failure.** Something you pushed broke. Said at the
  configured level, because a broken build is usually worth interrupting for.
- **failure → success.** It's fixed. Said quietly — this is closure on a notice
  you already got, not a new problem.

A run still in progress is never reported. It has no conclusion yet, and
"CI is running" is not information anyone acts on.

**Why `run_id` is part of the state.** Conclusion alone would miss a re-run:
push a fix, CI fails again for a different reason, and `failure == failure`
would stay silent. Keying on the run makes each finished run its own event.
"""

from ada import dev
from ada.heartbeat.notices import add_notice

CHECK_NAME = "ci"


def check_ci(params: dict) -> None:
    repos = dev.repos()
    if not repos:
        return

    state = dev.load_state()
    changed = False

    for path in repos:
        run = dev.ci_state(path)
        if not run:
            continue  # no Actions, no remote, or gh can't answer — not an incident
        if run.get("status") != "completed" or not run.get("conclusion"):
            continue  # still running; wait for it to mean something

        previous = state.get(path.name) or {}
        if previous.get("run_id") == run["id"]:
            continue  # already accounted for this exact run

        conclusion = run["conclusion"]
        was = previous.get("conclusion")

        # First sighting of a repo is not a transition. Record it silently,
        # otherwise enabling this check announces the state of every project at
        # once — including four-month-old failures nobody asked about now.
        if previous:
            if conclusion != "success" and was == "success":
                add_notice(
                    CHECK_NAME,
                    f"{path.name}: CI {conclusion} on {run.get('branch') or 'its branch'}"
                    + (f" — {run['title']}" if run.get("title") else ""),
                    level=params.get("level", "urgent"),
                )
            elif conclusion == "success" and was and was != "success":
                add_notice(
                    CHECK_NAME,
                    f"{path.name}: CI is green again.",
                    level=params.get("recovery_level", "normal"),
                )

        state[path.name] = {"run_id": run["id"], "conclusion": conclusion}
        changed = True

    if changed:
        dev.save_state(state)
