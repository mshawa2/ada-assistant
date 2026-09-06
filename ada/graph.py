"""Keeping the knowledge graph current without rebuilding it constantly.

The graph spans five repositories and the vault — around 7,000 nodes. Built
once, it immediately starts going stale: eighteen hours after the last build,
`ada-assistant` alone had six commits the graph knew nothing about, so asking
it "what calls this" answered about last night's code.

Rebuilding everything on a timer would fix that and waste most of its effort,
because most projects are untouched most days. So the unit of work here is a
**fingerprint comparison**, not a schedule:

- a repository fingerprints as its `HEAD` commit — one cheap subprocess, and it
  changes exactly when the code does;
- the vault fingerprints as its file count and newest modification time, since
  markdown has no commits and a person edits it in Obsidian at random.

Only what moved is rebuilt, and the merged graph is re-merged only if something
was. On a quiet day the whole check is six `git rev-parse` calls and a
directory walk.

**It refreshes; it never creates.** A graph that does not exist yet is left
alone, because `graph_build` is gated — building one is minutes of CPU and a
new directory inside someone's repository, and that should stay a decision a
person makes. Refreshing a graph they already chose to create is a different
act, and the one this automates.

Everything is local: `graphify update` parses syntax trees on this machine with
no model call and no network, so a rebuild costs CPU and nothing else.
"""

import subprocess
import time
from pathlib import Path

from ada import dev, store, vault

_STATE = store.DATA_DIR / "graph_state.json"

# Generous: a large repository's parse is seconds, but a cold filesystem can
# make the first one slow, and a timeout mid-write leaves a half-built graph.
_BUILD_TIMEOUT = 600
_MERGE_TIMEOUT = 300


def repo_fingerprint(path: Path) -> str | None:
    """A repo's HEAD, or None if it can't be read."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def vault_fingerprint() -> str:
    """File count and newest mtime across the vault's markdown.

    Not a hash of the contents: the point is to be cheap enough to run often,
    and this changes on every edit, addition and deletion that matters. The
    graph output directory is skipped, or rebuilding the graph would change the
    fingerprint and ask to be rebuilt again, forever.
    """
    newest = 0.0
    count = 0
    for path in vault.VAULT_DIR.rglob("*.md"):
        if "graphify-out" in path.parts:
            continue
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
        count += 1
    return f"{count}:{newest:.0f}"


def _run(args: list[str], timeout: int) -> bool:
    binary = dev.graphify_bin()
    if binary is None:
        return False
    try:
        done = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def stale() -> dict:
    """What has moved since its graph was last built.

    Returns {"repos": [Path, ...], "vault": bool}. A repo with no graph is not
    stale — it is unbuilt, which is a decision for the user rather than a
    maintenance task.
    """
    state = store.read_json(_STATE, {})
    known = state.get("repos") or {}

    moved = []
    for path in dev.repos():
        if not dev.has_graph(path):
            continue  # never built; not this check's business
        current = repo_fingerprint(path)
        if current and known.get(path.name) != current:
            moved.append(path)

    vault_graph = vault.VAULT_DIR / "graphify-out" / "graph.json"
    vault_moved = vault_graph.is_file() and state.get("vault") != vault_fingerprint()

    return {"repos": moved, "vault": vault_moved}


def merged_path() -> Path:
    return vault.VAULT_DIR / "graphify-out" / "merged-graph.json"


def rebuild(limit: int = 2) -> dict:
    """Refresh what has changed, then re-merge if anything did.

    `limit` caps how many repositories are rebuilt in one pass. The heartbeat
    runs checks sequentially on a single thread, so a pass that rebuilt five
    large repositories at once would hold that thread and delay a reminder due
    in the meantime. Whatever is skipped is still stale next time, and is
    picked up then.
    """
    work = stale()
    state = store.read_json(_STATE, {})
    known = dict(state.get("repos") or {})

    rebuilt = []
    for path in work["repos"][:limit]:
        if _run(["update", str(path)], _BUILD_TIMEOUT):
            fingerprint = repo_fingerprint(path)
            if fingerprint:
                known[path.name] = fingerprint
            rebuilt.append(path.name)

    vault_rebuilt = False
    if work["vault"] and _run(["update", str(vault.VAULT_DIR)], _BUILD_TIMEOUT):
        # Read the fingerprint *after* the build: graphify writes into the
        # vault, and stamping the earlier value would leave it looking stale
        # forever.
        state["vault"] = vault_fingerprint()
        vault_rebuilt = True

    merged = False
    if rebuilt or vault_rebuilt:
        graphs = [vault.VAULT_DIR / "graphify-out" / "graph.json"]
        graphs += [dev.graph_dir(p) / "graph.json" for p in dev.repos() if dev.has_graph(p)]
        present = [str(g) for g in graphs if g.is_file()]
        if len(present) > 1 and _run(
            ["merge-graphs", *present, "--out", str(merged_path())], _MERGE_TIMEOUT
        ):
            state["merged_at"] = time.time()
            merged = True

    state["repos"] = known
    store.write_json(_STATE, state)
    return {
        "rebuilt": rebuilt,
        "vault": vault_rebuilt,
        "merged": merged,
        "remaining": max(0, len(work["repos"]) - limit),
    }
