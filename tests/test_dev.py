"""Repo state, and the CI check that must not repeat itself.

Nothing here shells out for real. `dev._run` and `dev.ci_state` are the two
seams every probe goes through, so faking them covers git, gh and PAUL without
a network call or a fixture repo full of commits.

The CI tests are the point of this file. `sync_check` once wrote the same
sentence into the notice log twenty times because it reported *state* instead of
*change*, and a red build is exactly that shape — it stays red until someone
fixes it. So the cases below are mostly about staying quiet.
"""

import pytest

from ada import dev
from ada.heartbeat import ci_check, notices


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """One fake repo Ada watches, with no real git or gh behind it."""
    path = tmp_path / "instaplan"
    (path / ".git").mkdir(parents=True)
    monkeypatch.setattr(dev, "repos", lambda: [path])
    return path


def fake_run(run_id, conclusion, status="completed"):
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "title": "Some commit",
        "branch": "master",
    }


# --- repo discovery ----------------------------------------------------------


def test_repos_walks_two_levels_for_domain_folders(tmp_path, monkeypatch):
    (tmp_path / "ai" / "ada-assistant" / ".git").mkdir(parents=True)
    (tmp_path / "web" / "site" / ".git").mkdir(parents=True)
    (tmp_path / "toplevel" / ".git").mkdir(parents=True)
    (tmp_path / "web" / "not-a-repo").mkdir(parents=True)
    monkeypatch.setattr(dev, "config", lambda: {"root": str(tmp_path), "repos": [], "graph_dir": "g"})

    assert [p.name for p in dev.repos()] == ["ada-assistant", "toplevel", "site"]


def test_explicit_repo_list_wins_over_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dev, "config", lambda: {"root": "/nope", "repos": [str(tmp_path / "x")], "graph_dir": "g"}
    )
    assert [p.name for p in dev.repos()] == ["x"]


def test_missing_root_yields_no_repos(monkeypatch):
    monkeypatch.setattr(dev, "config", lambda: {"root": "/definitely/not/here", "repos": [], "graph_dir": "g"})
    assert dev.repos() == []


# --- git state ---------------------------------------------------------------


def test_git_state_reads_branch_dirty_and_unpushed(tmp_path, monkeypatch):
    answers = {"rev-parse": "main", "status": " M a.py\n?? b.py", "rev-list": "3"}
    monkeypatch.setattr(
        dev, "_run", lambda args, cwd, timeout: next(v for k, v in answers.items() if k in args)
    )
    assert dev.git_state(tmp_path) == {"branch": "main", "dirty": 2, "unpushed": 3}


def test_no_upstream_reports_unpushed_as_none_not_zero(tmp_path, monkeypatch):
    """"Nothing to push" and "nowhere to push to" are different situations."""
    monkeypatch.setattr(
        dev,
        "_run",
        lambda args, cwd, timeout: "main" if "rev-parse" in args else ("" if "status" in args else None),
    )
    assert dev.git_state(tmp_path)["unpushed"] is None


def test_git_failures_do_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "_run", lambda args, cwd, timeout: None)
    assert dev.git_state(tmp_path) == {"branch": None, "dirty": None, "unpushed": None}


# --- PAUL state --------------------------------------------------------------


def test_paul_state_absent_when_not_initialised(tmp_path):
    assert dev.paul_state(tmp_path) is None


def test_paul_state_reads_loop_position(tmp_path):
    (tmp_path / ".paul").mkdir()
    (tmp_path / ".paul" / "paul.toml").write_text(
        'status = "active"\n\n[loop]\nposition = "APPLY"\nplan = "0003-auth"\n\n'
        '[phase]\nname = "beta"\n\n[stats]\nlast_activity = "2026-09-05T12:00:00-05:00"\n'
    )
    state = dev.paul_state(tmp_path)

    assert state["position"] == "APPLY"
    assert state["plan"] == "0003-auth"
    assert state["phase"] == "beta"
    assert state["last_activity"] == "2026-09-05T12:00:00-05:00"


def test_absent_plan_key_means_idle(tmp_path):
    """PAUL omits the key rather than writing empty — TOML has no null."""
    (tmp_path / ".paul").mkdir()
    (tmp_path / ".paul" / "paul.toml").write_text('[loop]\nposition = "IDLE"\n')
    assert dev.paul_state(tmp_path)["plan"] is None


def test_malformed_paul_toml_is_not_fatal(tmp_path):
    (tmp_path / ".paul").mkdir()
    (tmp_path / ".paul" / "paul.toml").write_text("this is not = = toml [[[")
    assert dev.paul_state(tmp_path) is None


# --- describe ----------------------------------------------------------------


def test_describe_says_clean_when_nothing_is_wrong():
    record = {
        "name": "instaplan",
        "git": {"branch": "master", "dirty": 0, "unpushed": 0},
        "ci": {"status": "completed", "conclusion": "success"},
        "paul": None,
    }
    assert dev.describe(record) == "instaplan: master"


def test_describe_surfaces_problems():
    record = {
        "name": "site",
        "git": {"branch": "main", "dirty": 2, "unpushed": 1},
        "ci": {"status": "completed", "conclusion": "failure"},
        "paul": {"position": "APPLY", "plan": "0003"},
    }
    line = dev.describe(record)

    assert "2 uncommitted" in line and "1 unpushed" in line
    assert "CI failure" in line and "PAUL APPLY: 0003" in line


# --- the CI check: transitions only -----------------------------------------


def test_first_sighting_is_recorded_silently(repo, monkeypatch, sent_notifications):
    """Enabling the check must not announce every repo's current state."""
    monkeypatch.setattr(dev, "ci_state", lambda p: fake_run("1", "failure"))
    ci_check.check_ci({})

    assert sent_notifications == []
    assert dev.load_state()["instaplan"] == {"run_id": "1", "conclusion": "failure"}


def test_green_to_failure_notifies(repo, monkeypatch, sent_notifications):
    dev.save_state({"instaplan": {"run_id": "1", "conclusion": "success"}})
    monkeypatch.setattr(dev, "ci_state", lambda p: fake_run("2", "failure"))
    ci_check.check_ci({})

    assert len(sent_notifications) == 1
    assert "CI failure" in sent_notifications[0]["text"]


def test_failure_to_green_is_logged_but_not_pushed(repo, monkeypatch, sent_notifications):
    """Recovery is closure on a notice you already got, not a new interruption.

    It's raised at `normal`, which is outside the configured push levels, so it
    lands in the notice log without buzzing a phone. Asserting on the log rather
    than on pushes is what makes that distinction visible.
    """
    dev.save_state({"instaplan": {"run_id": "1", "conclusion": "failure"}})
    monkeypatch.setattr(dev, "ci_state", lambda p: fake_run("2", "success"))
    ci_check.check_ci({})

    logged = notices.list_notices()
    assert len(logged) == 1
    assert "green again" in logged[0]["text"]
    assert logged[0]["level"] == "normal"
    assert sent_notifications == []


def test_same_run_seen_twice_is_silent(repo, monkeypatch, sent_notifications):
    """The bug this check exists to avoid: repeating permanent state."""
    dev.save_state({"instaplan": {"run_id": "1", "conclusion": "success"}})
    monkeypatch.setattr(dev, "ci_state", lambda p: fake_run("2", "failure"))

    for _ in range(6):
        ci_check.check_ci({})

    assert len(sent_notifications) == 1


def test_a_second_distinct_failure_is_news_again(repo, monkeypatch, sent_notifications):
    """A re-run that fails differently is a new event, not an echo."""
    dev.save_state({"instaplan": {"run_id": "1", "conclusion": "success"}})
    monkeypatch.setattr(dev, "ci_state", lambda p: fake_run("2", "failure"))
    ci_check.check_ci({})
    monkeypatch.setattr(dev, "ci_state", lambda p: fake_run("3", "success"))
    ci_check.check_ci({})
    monkeypatch.setattr(dev, "ci_state", lambda p: fake_run("4", "failure"))
    ci_check.check_ci({})

    # Three transitions, three notices: broke, recovered, broke again. Only the
    # two failures are pushed — the recovery is logged at `normal`.
    assert [n["text"] for n in notices.list_notices()] == [
        "instaplan: CI failure on master — Some commit",
        "instaplan: CI is green again.",
        "instaplan: CI failure on master — Some commit",
    ]
    assert len(sent_notifications) == 2


def test_run_still_in_progress_is_ignored(repo, monkeypatch, sent_notifications):
    dev.save_state({"instaplan": {"run_id": "1", "conclusion": "success"}})
    monkeypatch.setattr(dev, "ci_state", lambda p: fake_run("2", None, status="in_progress"))
    ci_check.check_ci({})

    assert sent_notifications == []
    assert dev.load_state()["instaplan"]["run_id"] == "1"


def test_repo_without_ci_is_not_an_incident(repo, monkeypatch, sent_notifications):
    monkeypatch.setattr(dev, "ci_state", lambda p: None)
    ci_check.check_ci({})

    assert sent_notifications == []


def test_no_repos_does_nothing(monkeypatch, sent_notifications):
    monkeypatch.setattr(dev, "repos", lambda: [])
    ci_check.check_ci({})
    assert sent_notifications == []


# --- graph_query routing -----------------------------------------------------


def test_graph_query_routes_all_to_the_merged_graph(repo, tmp_path, monkeypatch):
    """'all' is the only value that crosses from code into the vault."""
    from ada.tools import dev_tools

    merged = tmp_path / "merged-graph.json"
    merged.write_text("{}")
    monkeypatch.setattr(dev, "graphify_bin", lambda: "/bin/echo")
    monkeypatch.setattr(dev, "merged_graph", lambda: merged)

    out = dev_tools.graph_query("all", "anything", mode="hubs")
    assert str(merged) in out


def test_graph_query_says_so_when_no_merged_graph_exists(repo, monkeypatch):
    from ada.tools import dev_tools

    monkeypatch.setattr(dev, "graphify_bin", lambda: "/bin/echo")
    monkeypatch.setattr(dev, "merged_graph", lambda: None)

    assert "No merged graph yet" in dev_tools.graph_query("all", "x")


def test_graph_query_rejects_an_unknown_project(repo, monkeypatch):
    from ada.tools import dev_tools

    monkeypatch.setattr(dev, "graphify_bin", lambda: "/bin/echo")
    assert "No project matching" in dev_tools.graph_query("nope", "x")


def test_graph_query_reports_a_missing_per_repo_graph(repo, monkeypatch):
    from ada.tools import dev_tools

    monkeypatch.setattr(dev, "graphify_bin", lambda: "/bin/echo")
    assert "No knowledge graph for instaplan" in dev_tools.graph_query("instaplan", "x")


def test_graph_query_without_graphify_installed_is_actionable(repo, monkeypatch):
    from ada.tools import dev_tools

    monkeypatch.setattr(dev, "graphify_bin", lambda: None)
    assert "uv tool install graphifyy" in dev_tools.graph_query("all", "x")
