"""The durability layer everything else sits on, plus the audit log's rotation."""

import json

from ada import audit, store
from tests.conftest import TEST_DATA_DIR


def test_read_json_returns_default_when_missing():
    assert store.read_json(TEST_DATA_DIR / "nope.json", []) == []
    assert store.read_json(TEST_DATA_DIR / "nope.json", {"a": 1}) == {"a": 1}


def test_write_then_read_round_trips():
    path = TEST_DATA_DIR / "round.json"
    store.write_json(path, [{"id": 1, "text": "hello"}])
    assert store.read_json(path, []) == [{"id": 1, "text": "hello"}]


def test_write_creates_missing_parent_directories():
    path = TEST_DATA_DIR / "nested" / "deep" / "x.json"
    store.write_json(path, {"ok": True})
    assert store.read_json(path, {}) == {"ok": True}


def test_write_leaves_no_temp_files_behind():
    path = TEST_DATA_DIR / "clean.json"
    store.write_json(path, {"a": 1})
    store.write_json(path, {"a": 2})
    leftovers = [p.name for p in TEST_DATA_DIR.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_write_replaces_rather_than_truncating():
    """The whole point of the atomic write: the old contents stay readable
    right up until the new ones land, so a reader never sees a half-file."""
    path = TEST_DATA_DIR / "atomic.json"
    store.write_json(path, {"version": 1})

    original = path.read_text()
    store.write_json(path, {"version": 2})

    assert json.loads(original) == {"version": 1}
    assert store.read_json(path, {}) == {"version": 2}


def test_corrupt_file_is_quarantined_not_silently_dropped(capsys):
    path = TEST_DATA_DIR / "broken.json"
    path.write_text('[{"id": 1, "text": "half a fi')

    result = store.read_json(path, [])

    assert result == []
    assert (TEST_DATA_DIR / "broken.json.corrupt").exists(), "bad bytes must stay recoverable by hand"
    assert not path.exists()
    assert "unreadable" in capsys.readouterr().out


def test_lock_is_shared_per_path_not_per_caller():
    """Two modules pointing at the same file must serialize against each
    other — the bug the old per-module private locks couldn't prevent."""
    a = store.lock_for(TEST_DATA_DIR / "shared.json")
    b = store.lock_for(TEST_DATA_DIR / "shared.json")
    c = store.lock_for(TEST_DATA_DIR / "other.json")

    assert a is b
    assert a is not c


def test_lock_is_reentrant():
    lock = store.lock_for(TEST_DATA_DIR / "reentrant.json")
    with lock:
        with lock:  # would deadlock on a plain Lock
            pass


def test_data_dir_honours_the_env_override():
    assert store.DATA_DIR == TEST_DATA_DIR, "tests must never touch the real data/ directory"


# --- the audit log ----------------------------------------------------------


def test_audit_writes_one_json_object_per_line():
    audit.log("tool_call", tool="list_reminders", result="No reminders.")
    audit.log("model_call", stop_reason="end_turn")

    lines = (TEST_DATA_DIR / "audit.log").read_text().splitlines()

    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "tool_call"
    assert "time" in json.loads(lines[0])


def test_audit_survives_a_value_that_will_not_serialize():
    """The log is how you find out what a tool did — an odd argument must not
    be able to take down the very record you'd go read."""
    audit.log("tool_call", tool="weird", input={"when": object()})

    assert len(audit.tail()) == 1


def test_audit_tail_is_newest_first_and_skips_partial_lines():
    audit.log("first", n=1)
    audit.log("second", n=2)
    with (TEST_DATA_DIR / "audit.log").open("a") as f:
        f.write('{"time": 1.0, "event": "torn half-writ')

    entries = audit.tail()

    assert [e["event"] for e in entries] == ["second", "first"]


def test_audit_rotates_instead_of_growing_without_bound(monkeypatch):
    monkeypatch.setattr(audit, "MAX_BYTES", 500)

    for i in range(200):
        audit.log("filler", i=i, padding="x" * 50)

    log_file = TEST_DATA_DIR / "audit.log"
    assert log_file.stat().st_size < audit.MAX_BYTES * 2
    assert (TEST_DATA_DIR / "audit.log.1").exists(), "history must roll aside, not vanish"
    assert not (TEST_DATA_DIR / f"audit.log.{audit.KEEP + 1}").exists(), "the oldest generation is dropped"


def test_audit_tail_is_empty_before_anything_is_logged():
    assert audit.tail() == []
