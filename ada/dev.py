"""Ada's read-only view of the development work happening in her user's repos.

This is the kernel the dev tools and the CI heartbeat check sit on. It answers
one question — *where does each project actually stand right now* — by reading
three sources that already exist rather than inventing a fourth:

- **git**, for what's on disk: branch, uncommitted files, unpushed commits.
- **`gh`**, for what CI thinks: the latest run's conclusion.
- **`.paul/paul.toml`**, for where PAUL's plan/apply/unify loop is parked.

Nothing here writes to a repo. That's deliberate and matches the autonomy split
the user chose: reading is unattended, acting is gated. The only thing this
module persists is `data/dev_state.json`, and only so the heartbeat can tell a
*change* from a repeat — a CI failure should be announced once, not every tick.

**Why everything is best-effort.** A briefing is worth having when the network
is down, `gh` is logged out, or a repo has no PAUL state. So every probe
returns a partial answer instead of raising, and the caller renders what it
got. The failure mode to avoid is an assistant that says nothing because one
of four repos couldn't be reached.

**Why subprocess and not a git library.** Ada already shells out for location,
notifications and iMessage; `git` and `gh` are on the PATH, authenticated, and
their porcelain output is stable. A dependency would buy nothing.
"""

import json
import os
import subprocess
import tomllib
from pathlib import Path

from ada import settings, store

_STATE = store.DATA_DIR / "dev_state.json"

# Long enough for a cold `gh` call over a slow link, short enough that four
# repos can't stall a heartbeat tick past its interval.
_GIT_TIMEOUT = 10
_GH_TIMEOUT = 20

DEFAULT_ROOT = "~/IdeaProjects"


def _run(args: list[str], cwd: Path, timeout: int) -> str | None:
    """Run a command, returning stdout or None if it failed in any way.

    Every failure collapses to None on purpose: a missing binary, a non-zero
    exit, a timeout and a git repo that isn't one are all "no answer" as far as
    a briefing is concerned, and the caller has nothing different to do about
    any of them.
    """
    try:
        done = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def config() -> dict:
    """The `dev` block, with defaults for a machine that's never set one."""
    try:
        block = settings.load().get("dev", {}) or {}
    except Exception:
        block = {}
    return {
        "root": block.get("root", DEFAULT_ROOT),
        "repos": block.get("repos", []),
        "graph_dir": block.get("graph_dir", "graphify-out"),
    }


def repos() -> list[Path]:
    """Every git repo Ada watches.

    An explicit `dev.repos` list wins. Otherwise the root is walked two levels
    deep, which is exactly the shape of `~/IdeaProjects/<domain>/<project>`
    while still finding a repo placed directly at the root.
    """
    cfg = config()
    if cfg["repos"]:
        return [Path(p).expanduser() for p in cfg["repos"]]

    root = Path(cfg["root"]).expanduser()
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / ".git").exists():
            found.append(child)
            continue
        for grandchild in sorted(child.iterdir()):
            if grandchild.is_dir() and (grandchild / ".git").exists():
                found.append(grandchild)
    return found


def git_state(path: Path) -> dict:
    """Branch, uncommitted file count, and unpushed commits.

    `unpushed` is None rather than 0 when there's no upstream — "nothing to
    push" and "nowhere to push to" are different situations, and only one of
    them is worth mentioning to the user.
    """
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path, _GIT_TIMEOUT)
    porcelain = _run(["git", "status", "--porcelain"], path, _GIT_TIMEOUT)
    ahead = _run(["git", "rev-list", "--count", "@{u}..HEAD"], path, _GIT_TIMEOUT)

    return {
        "branch": branch,
        "dirty": len([ln for ln in porcelain.splitlines() if ln]) if porcelain is not None else None,
        "unpushed": int(ahead) if ahead and ahead.isdigit() else None,
    }


def paul_state(path: Path) -> dict | None:
    """PAUL's loop position for this repo, or None if PAUL doesn't manage it.

    Reads `paul.toml` rather than `STATE.md` because the toml is the machine
    surface PAUL maintains for exactly this purpose; STATE.md is prose written
    for a human. A missing `loop.plan` key means idle — PAUL omits the key
    instead of writing an empty string, since TOML has no null.
    """
    toml_path = path / ".paul" / "paul.toml"
    if not toml_path.is_file():
        return None
    try:
        data = tomllib.loads(toml_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None  # a half-written toml is not worth failing a briefing over

    loop = data.get("loop", {}) or {}
    phase = data.get("phase", {}) or {}
    return {
        "position": loop.get("position"),
        "plan": loop.get("plan"),  # absent => idle
        "phase": phase.get("name") or phase.get("number"),
        "status": data.get("status"),
        "last_activity": (data.get("stats", {}) or {}).get("last_activity"),
    }


def ci_state(path: Path) -> dict | None:
    """The most recent CI run for this repo, via `gh`.

    None covers every "can't tell" case — no remote, no GitHub, not logged in,
    Actions never configured. A repo with no CI is not a problem to report.
    """
    raw = _run(
        ["gh", "run", "list", "--limit", "1",
         "--json", "databaseId,status,conclusion,displayTitle,headBranch"],
        path,
        _GH_TIMEOUT,
    )
    if not raw:
        return None
    try:
        runs = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not runs:
        return None

    run = runs[0]
    return {
        "id": str(run.get("databaseId", "")),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "title": run.get("displayTitle"),
        "branch": run.get("headBranch"),
    }


def snapshot(include_ci: bool = True) -> list[dict]:
    """One record per watched repo. The single call both tools and the check use.

    `include_ci` exists because a `gh` round trip per repo is the slow part;
    a caller that only wants local state shouldn't pay for it.
    """
    out = []
    for path in repos():
        record = {"name": path.name, "path": str(path), "git": git_state(path), "paul": paul_state(path)}
        record["ci"] = ci_state(path) if include_ci else None
        out.append(record)
    return out


def load_state() -> dict:
    return store.read_json(_STATE, {})


def save_state(state: dict) -> None:
    store.write_json(_STATE, state)


def describe(record: dict) -> str:
    """One repo as a single readable line.

    Deliberately omits anything that is fine: a clean repo on its default
    branch with green CI should be short, so the lines that *do* say something
    stand out in a briefing.
    """
    bits = []
    git = record.get("git") or {}
    if git.get("branch"):
        bits.append(git["branch"])
    if git.get("dirty"):
        bits.append(f"{git['dirty']} uncommitted")
    if git.get("unpushed"):
        bits.append(f"{git['unpushed']} unpushed")

    ci = record.get("ci") or {}
    if ci.get("status") and ci["status"] != "completed":
        bits.append("CI running")
    elif ci.get("conclusion") and ci["conclusion"] != "success":
        bits.append(f"CI {ci['conclusion']}")

    paul = record.get("paul") or {}
    if paul.get("plan"):
        bits.append(f"PAUL {paul.get('position') or 'in loop'}: {paul['plan']}")
    elif paul.get("position"):
        bits.append(f"PAUL {paul['position']}")

    return f"{record['name']}: " + (", ".join(bits) if bits else "clean")


def graph_dir(path: Path) -> Path:
    return path / config()["graph_dir"]


def has_graph(path: Path) -> bool:
    return (graph_dir(path) / "graph.json").is_file()


def graphify_bin() -> str | None:
    """Where the graphify CLI lives, or None if it isn't installed.

    `uv tool install` puts it in `~/.local/bin`, which a launchd agent's
    minimal PATH does not include — so the heartbeat would see no graphify
    while an interactive shell sees one. Checking the known location directly
    avoids that split.
    """
    from shutil import which

    found = which("graphify")
    if found:
        return found
    fallback = Path(os.environ.get("HOME", "~")).expanduser() / ".local" / "bin" / "graphify"
    return str(fallback) if fallback.is_file() else None
