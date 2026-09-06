"""Keeping the knowledge graph current.

The interesting behaviour is all about *not* working: rebuilding only what
moved, skipping what was never built, and capping how much happens in one pass
so the heartbeat thread stays free.

`graph._run` is the single seam where graphify would be executed, so faking it
covers every subprocess without parsing a real repository.
"""

import time

import pytest

from ada import dev, graph, store, vault
from ada.heartbeat import graph_check, notices


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Two repos with graphs, one without, and a vault with a graph."""
    repos = []
    for name, built in (("alpha", True), ("beta", True), ("unbuilt", False)):
        path = tmp_path / name
        (path / ".git").mkdir(parents=True)
        if built:
            out = path / "graphify-out"
            out.mkdir()
            (out / "graph.json").write_text("{}")
        repos.append(path)

    monkeypatch.setattr(dev, "repos", lambda: repos)
    monkeypatch.setattr(dev, "has_graph", lambda p: (p / "graphify-out" / "graph.json").is_file())
    monkeypatch.setattr(dev, "graph_dir", lambda p: p / "graphify-out")

    vault.ensure()
    (vault.VAULT_DIR / "graphify-out").mkdir(parents=True, exist_ok=True)
    (vault.VAULT_DIR / "graphify-out" / "graph.json").write_text("{}")
    (vault.VAULT_DIR / "notes" / "a.md").write_text("# a")

    # Every repo reports a stable HEAD unless a test changes it.
    heads = {"alpha": "aaa", "beta": "bbb", "unbuilt": "ccc"}
    monkeypatch.setattr(graph, "repo_fingerprint", lambda p: heads.get(p.name))
    return {"repos": repos, "heads": heads}


@pytest.fixture
def ran(monkeypatch):
    """Record graphify invocations instead of running them."""
    calls = []
    monkeypatch.setattr(graph, "_run", lambda args, timeout: calls.append(args) or True)
    return calls


# --- what counts as stale ----------------------------------------------------


def test_everything_with_a_graph_is_stale_on_a_first_run(workspace):
    work = graph.stale()

    assert {p.name for p in work["repos"]} == {"alpha", "beta"}
    assert work["vault"] is True


def test_a_repo_without_a_graph_is_never_stale(workspace):
    """Unbuilt is a decision for the user, not a maintenance task."""
    assert "unbuilt" not in {p.name for p in graph.stale()["repos"]}


def test_nothing_is_stale_after_a_rebuild(workspace, ran):
    graph.rebuild(limit=5)
    work = graph.stale()

    assert work["repos"] == []
    assert work["vault"] is False


def test_a_new_commit_makes_one_repo_stale_again(workspace, ran):
    graph.rebuild(limit=5)
    workspace["heads"]["alpha"] = "a-new-commit"

    assert [p.name for p in graph.stale()["repos"]] == ["alpha"]


def test_an_edited_note_makes_the_vault_stale_again(workspace, ran):
    graph.rebuild(limit=5)
    time.sleep(0.01)
    (vault.VAULT_DIR / "notes" / "b.md").write_text("# b")

    assert graph.stale()["vault"] is True


def test_the_graphs_own_output_does_not_make_the_vault_stale(workspace):
    """Otherwise a rebuild would dirty the fingerprint and loop forever."""
    before = graph.vault_fingerprint()
    (vault.VAULT_DIR / "graphify-out" / "GRAPH_REPORT.md").write_text("# report")

    assert graph.vault_fingerprint() == before


# --- rebuilding --------------------------------------------------------------


def test_only_stale_repos_are_rebuilt(workspace, ran):
    graph.rebuild(limit=5)
    workspace["heads"]["beta"] = "moved"
    ran.clear()

    result = graph.rebuild(limit=5)

    assert result["rebuilt"] == ["beta"]
    # Only the `update` calls prove what was re-parsed. The merge names every
    # graph path including alpha's, which is correct — a merge that dropped the
    # untouched repos would shrink the graph to whatever changed today.
    updated = [c for c in ran if c[0] == "update"]
    assert not any("alpha" in " ".join(c) for c in updated)


def test_a_pass_is_capped_so_the_heartbeat_stays_free(workspace, ran):
    result = graph.rebuild(limit=1)

    assert len(result["rebuilt"]) == 1
    assert result["remaining"] == 1


def test_what_is_skipped_is_picked_up_next_time(workspace, ran):
    first = graph.rebuild(limit=1)
    second = graph.rebuild(limit=1)

    assert set(first["rebuilt"]) | set(second["rebuilt"]) == {"alpha", "beta"}


def test_the_merge_runs_when_something_was_rebuilt(workspace, ran):
    result = graph.rebuild(limit=5)

    assert result["merged"] is True
    assert any(c[0] == "merge-graphs" for c in ran)


def test_nothing_merges_when_nothing_changed(workspace, ran):
    graph.rebuild(limit=5)
    ran.clear()

    result = graph.rebuild(limit=5)

    assert result["merged"] is False
    assert ran == []


def test_a_failed_build_does_not_record_a_fingerprint(workspace, monkeypatch):
    monkeypatch.setattr(graph, "_run", lambda args, timeout: False)
    graph.rebuild(limit=5)

    # Still stale, so the next pass tries again rather than assuming success.
    assert {p.name for p in graph.stale()["repos"]} == {"alpha", "beta"}


# --- the check ---------------------------------------------------------------


def test_the_check_is_silent_on_a_routine_rebuild(workspace, ran):
    graph_check.check_graph({})
    assert notices.list_notices() == []


def test_the_check_speaks_after_a_long_drift(workspace, ran):
    state = store.read_json(graph._STATE, {})
    state["merged_at"] = time.time() - 20 * 3600
    store.write_json(graph._STATE, state)

    graph_check.check_graph({})

    said = notices.list_notices()
    assert len(said) == 1
    assert "refreshed after 20 hours" in said[0]["text"]


def test_the_check_respects_its_own_interval(workspace, ran):
    graph_check.check_graph({})
    ran.clear()
    graph_check.check_graph({})

    assert ran == []


def test_the_interval_is_stamped_before_the_work(workspace, monkeypatch):
    """A rebuild that dies mid-parse must not retry on every tick."""

    def boom(args, timeout):
        raise RuntimeError("graphify fell over")

    monkeypatch.setattr(graph, "_run", boom)
    with pytest.raises(RuntimeError):
        graph_check.check_graph({})

    assert store.read_json(graph._STATE, {}).get("checked_at")
