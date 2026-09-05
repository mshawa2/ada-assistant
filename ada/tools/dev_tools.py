"""Ada's window onto development work: repo state, and the knowledge graph.

Named `dev_tools` for the same reason as `jobs_tool` and `calendar_tool` —
`ada/dev.py` is the kernel this wraps, and having both answer to `dev` on an
import is a confusing afternoon.

## The autonomy split

Reading is free; acting is gated. `dev_brief`, `dev_status` and `graph_query`
only look, so they run unattended and the heartbeat can use them. `graph_build`
writes a directory into one of the user's repos and burns real CPU, so it
carries its own confirmation.

That line is drawn where the *consequence* is, which is the same rule the rest
of Ada follows: `start_job` isn't gated because a job record is inert, while
the tools the job later calls are. Looking at a repo changes nothing about it.

## Why the graph is queried, not grepped

`graphify` builds a real graph — AST-parsed, deterministic, no embeddings — and
answers questions by traversing it. Ada asking "what calls this" gets an answer
with explained edges instead of a page of grep hits she then has to read. The
graph has to exist first, so every query says plainly when it doesn't rather
than returning an empty result that reads like "nothing found".
"""

import subprocess
from pathlib import Path

from ada import dev
from ada.tools.registry import tool

# Extraction is AST work over a whole repo. The big one here takes minutes, and
# a tool that gives up at 60s would look broken rather than slow.
_BUILD_TIMEOUT = 900
_QUERY_TIMEOUT = 120

_MODES = {
    "question": lambda q: ["query", q],
    "explain": lambda q: ["explain", q],
    "affected": lambda q: ["affected", q],
    "hubs": lambda _: ["god-nodes"],
}


def _resolve(repo: str | None) -> Path | None:
    """Match a repo by name, or fall back to the only one if there's just one.

    Substring matching is deliberate: the user says "instaplan", not
    "/Users/.../IdeaProjects/ai/instaplan", and a model relaying that shouldn't
    have to guess a path.
    """
    paths = dev.repos()
    if not paths:
        return None
    if not repo:
        return paths[0] if len(paths) == 1 else None
    wanted = repo.strip().lower()
    for path in paths:
        if path.name.lower() == wanted:
            return path
    for path in paths:
        if wanted in path.name.lower():
            return path
    return None


def _repo_names() -> str:
    return ", ".join(p.name for p in dev.repos()) or "none found"


@tool(
    name="dev_brief",
    description=(
        "Where every project the user works on currently stands: branch, uncommitted "
        "and unpushed work, the latest CI result, and PAUL's plan/apply/unify position.\n\n"
        "Use for questions about project status, broken builds, or unfinished work, and as "
        "the development half of a morning briefing. It only reads — nothing here "
        "changes a repo.\n\n"
        "Repos with nothing to say are reported as clean, so the lines that do say "
        "something are the ones worth reading out."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "include_ci": {
                "type": "boolean",
                "description": "Check GitHub Actions too. Slower (a network call per repo). Default true.",
            }
        },
    },
)
def dev_brief(include_ci: bool = True) -> str:
    records = dev.snapshot(include_ci=include_ci)
    if not records:
        return (
            "No repos found. Ada looks under ~/IdeaProjects by default — set "
            "`dev.root`, or list paths in `dev.repos`, to point her somewhere else."
        )
    return "\n".join(dev.describe(r) for r in records)


@tool(
    name="dev_status",
    description=(
        "Everything known about one project: git state, the latest CI run and its title, "
        "PAUL's loop position and current plan, and whether a knowledge graph has been "
        "built for it.\n\n"
        "Use when the user asks about a specific project rather than all of them. Run "
        "dev_brief first if you don't know which projects exist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Project name, e.g. 'instaplan'."}
        },
        "required": ["repo"],
    },
)
def dev_status(repo: str) -> str:
    path = _resolve(repo)
    if path is None:
        return f"No project matching '{repo}'. Known projects: {_repo_names()}."

    git = dev.git_state(path)
    paul = dev.paul_state(path)
    ci = dev.ci_state(path)

    lines = [f"{path.name} — {path}"]
    dirty = git.get("dirty")
    unpushed = git.get("unpushed")
    lines.append(
        f"  git: {git.get('branch') or 'unknown branch'}"
        + (f", {dirty} uncommitted" if dirty else ", clean")
        + (f", {unpushed} unpushed" if unpushed else "")
        + ("" if unpushed is not None else ", no upstream")
    )

    if ci:
        state = ci["conclusion"] or ci["status"] or "unknown"
        lines.append(f"  ci: {state}" + (f" — {ci['title']}" if ci.get("title") else ""))
    else:
        lines.append("  ci: no runs visible (no Actions, no remote, or gh not logged in)")

    if paul:
        plan = paul.get("plan")
        lines.append(
            f"  paul: {paul.get('position') or 'unknown'}" + (f", plan {plan}" if plan else ", idle")
        )
        if paul.get("last_activity"):
            lines.append(f"  paul last activity: {paul['last_activity']}")
    else:
        lines.append("  paul: not initialised here (run /paul:init in the repo)")

    lines.append(f"  graph: {'built' if dev.has_graph(path) else 'not built — use graph_build'}")
    return "\n".join(lines)


@tool(
    name="graph_query",
    description=(
        "Ask a project's knowledge graph a question instead of searching files. The graph "
        "is built from the code's syntax tree, so answers come with the connections that "
        "justify them.\n\n"
        "Modes:\n"
        "- 'question' (default): a natural-language question, answered by walking the graph\n"
        "- 'explain': what one thing is and what it connects to — pass its name\n"
        "- 'affected': what would break if the named thing changed\n"
        "- 'hubs': the most connected parts of the codebase, i.e. where the architecture lives\n\n"
        "Pass repo='all' to query the merged graph spanning every project *and* the user's "
        "vault of notes and decisions — that is the one that can connect a decision to the "
        "code it affects.\n\n"
        "Requires a graph for that project; if none exists, say so and offer graph_build. "
        "This only reads."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Project name, e.g. 'instaplan'."},
            "query": {
                "type": "string",
                "description": "The question, or the node name for 'explain' and 'affected'.",
            },
            "mode": {
                "type": "string",
                "enum": ["question", "explain", "affected", "hubs"],
                "description": "How to interrogate the graph. Default 'question'.",
            },
        },
        "required": ["repo", "query"],
    },
)
def graph_query(repo: str, query: str, mode: str = "question") -> str:
    binary = dev.graphify_bin()
    if binary is None:
        return "graphify isn't installed. Install it with: uv tool install graphifyy"

    # "all" crosses the boundary between code and knowledge: the merged graph
    # holds every repo plus the vault, so a decision page and the code it talks
    # about are reachable from each other.
    if repo.strip().lower() in {"all", "brain", "merged", "everything"}:
        graph = dev.merged_graph()
        if graph is None:
            return (
                "No merged graph yet. Build each project's graph with graph_build, "
                "then merge them with: graphify merge-graphs <each graph.json> "
                "--out ~/Vault/graphify-out/merged-graph.json"
            )
    else:
        path = _resolve(repo)
        if path is None:
            return f"No project matching '{repo}'. Known projects: {_repo_names()}, or 'all'."
        graph = dev.graph_dir(path) / "graph.json"
        if not graph.is_file():
            return (
                f"No knowledge graph for {path.name} yet — nothing to query. "
                "Build one with graph_build (it takes a while, and it asks first)."
            )

    build = _MODES.get(mode)
    if build is None:
        return f"Unknown mode '{mode}'. Use question, explain, affected or hubs."

    try:
        done = subprocess.run(
            [binary, *build(query), "--graph", str(graph)],
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"The graph query took longer than {_QUERY_TIMEOUT}s and was stopped."
    except OSError as exc:
        return f"Couldn't run graphify: {exc}"

    out = (done.stdout or "").strip()
    if done.returncode != 0:
        return f"graphify couldn't answer that: {(done.stderr or out or 'no output').strip()[:400]}"
    return out or "The graph returned nothing for that."


@tool(
    name="graph_build",
    description=(
        "Build or refresh a project's knowledge graph by re-reading its source. Everything "
        "is parsed locally from the syntax tree — no model calls, nothing leaves the machine.\n\n"
        "This is slow: minutes on a large project, and it writes a graphify-out/ directory "
        "into the repo. Do it when the user asks for a graph, or when graph_query reports "
        "there isn't one — not speculatively.\n\n"
        "Refreshing an existing graph after a big refactor is fine; it updates in place."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Project name, e.g. 'instaplan'."}
        },
        "required": ["repo"],
    },
    confirm_description=lambda args: (
        f"Build the knowledge graph for {args.get('repo', '?')}? This reads the whole "
        "project, takes minutes, and writes a graphify-out/ directory into the repo."
    ),
)
def graph_build(repo: str) -> str:
    path = _resolve(repo)
    if path is None:
        return f"No project matching '{repo}'. Known projects: {_repo_names()}."

    binary = dev.graphify_bin()
    if binary is None:
        return "graphify isn't installed. Install it with: uv tool install graphifyy"

    try:
        done = subprocess.run(
            [binary, "update", str(path)], capture_output=True, text=True, timeout=_BUILD_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return (
            f"Building the graph for {path.name} passed {_BUILD_TIMEOUT // 60} minutes and was "
            "stopped. A repo this size may need it run from a terminal instead."
        )
    except OSError as exc:
        return f"Couldn't run graphify: {exc}"

    if done.returncode != 0:
        return f"graphify couldn't build the graph: {(done.stderr or done.stdout or '').strip()[:400]}"

    if not dev.has_graph(path):
        return f"graphify finished but wrote no graph for {path.name}. Output: {(done.stdout or '')[-300:]}"
    return f"Knowledge graph built for {path.name}. Ask about it with graph_query."
