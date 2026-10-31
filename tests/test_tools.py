"""The tool registry contract, the safety config, and the stores' own rules."""

import pytest

from ada import safety
from ada.tools import call as call_tool
from ada.tools import memory, notes, reminders
from ada.tools import specs as tool_specs


def test_every_registered_tool_has_a_schema_the_api_will_accept():
    for spec in tool_specs():
        schema = spec["input_schema"]
        assert schema["type"] == "object", spec["name"]
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(properties)
        assert not missing, f"{spec['name']} requires undeclared properties: {missing}"


def test_every_registered_tool_describes_itself_for_a_reader():
    for spec in tool_specs():
        assert spec["description"].strip(), f"{spec['name']} has no description for the model to read"


def test_tool_names_are_unique():
    names = [spec["name"] for spec in tool_specs()]
    assert len(names) == len(set(names))


def test_calling_an_unknown_tool_raises():
    with pytest.raises(ValueError, match="No tool named"):
        call_tool("not_a_real_tool", {})


def test_the_irreversible_tools_are_gated():
    """safety_config.json is the live list — if a delete tool falls off it,
    Tier 6's whole guarantee quietly disappears."""
    gated = safety.load_confirmation_required_tools()
    assert "forget" in gated
    assert "delete_note" in gated
    assert safety.requires_confirmation("list_reminders") is False


def test_reminders_round_trip():
    reminders.add_reminder("water the plants", due="tomorrow")
    assert "water the plants" in reminders.list_reminders()

    stored = reminders.load_reminders()
    reminders.complete_reminder(stored[0]["id"])

    assert reminders.list_reminders() == "No reminders."
    assert "water the plants" in reminders.list_reminders(include_done=True)


def test_completing_a_missing_reminder_raises():
    with pytest.raises(ValueError, match="No reminder with id"):
        reminders.complete_reminder(42)


def test_memory_update_replaces_rather_than_duplicating():
    memory.remember("Favourite language is Python.")
    fact_id = memory.load_facts()[0]["id"]

    memory.update_memory(fact_id, "Favourite language is Rust.")

    facts = memory.load_facts()
    assert len(facts) == 1
    assert facts[0]["text"] == "Favourite language is Rust."


def test_forget_removes_only_the_named_fact():
    memory.remember("one")
    memory.remember("two")
    first, _second = memory.load_facts()

    memory.forget(first["id"])

    assert [f["text"] for f in memory.load_facts()] == ["two"]


@pytest.mark.parametrize("filename", ["../escaped.md", "../../etc/passwd", "/etc/passwd"])
def test_notes_reject_paths_outside_the_notes_directory(filename):
    with pytest.raises(ValueError, match="Invalid note filename"):
        notes.read_note(filename)
    with pytest.raises(ValueError, match="Invalid note filename"):
        notes.delete_note(filename)


def test_note_search_reports_file_and_line():
    notes._NOTES_DIR.mkdir(parents=True, exist_ok=True)
    (notes._NOTES_DIR / "ideas.md").write_text("first line\nsomething about otters\n")

    result = notes.search_notes("otters")

    assert "ideas.md:2" in result
    assert notes.search_notes("nothing here at all").startswith("No matches")


# --- the capability table in AGENT.md ---------------------------------------
#
# AGENT.md is the single source of truth and its table is the first thing
# anyone reads. It claimed to be generated from the registry and asserted
# against it; it was neither, and it had already drifted — 52 listed against 53
# registered, with `find_tool` missing entirely. A table that silently
# disagrees with the code is worse than no table, so here is the assertion the
# doc was already promising.

import re  # noqa: E402
from pathlib import Path  # noqa: E402

from ada import settings  # noqa: E402
from ada.tools.registry import get as get_tool  # noqa: E402

_AGENT_MD = Path(__file__).resolve().parent.parent / "AGENT.md"
_ROW = re.compile(r"\s*`(\w+)`\s*(⚠️\*|⚠️)?\s*$")


def _table_entries() -> dict[str, str | None]:
    """Every tool named in the capability table, mapped to its marker.

    Returns a dict rather than a set so a duplicate raises here — being listed
    under two areas is exactly the drift this is meant to catch.
    """
    lines = _AGENT_MD.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Area | Tools |"))

    entries: dict[str, str | None] = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for chunk in cells[1].split(","):
            match = _ROW.match(chunk)
            assert match, f"couldn't read {chunk!r} in the capability table"
            name, marker = match.group(1), match.group(2)
            assert name not in entries, f"{name} is listed twice in the capability table"
            entries[name] = marker
    return entries


def test_the_capability_table_lists_every_tool_exactly_once():
    listed = set(_table_entries())
    # A private module's tools (ada/extensions.py) are this machine's business,
    # not the public table's.
    registered = {
        spec["name"]
        for spec in tool_specs()
        if not get_tool(spec["name"]).func.__module__.startswith("ada.private.")
    }

    assert not registered - listed, f"registered but missing from AGENT.md: {sorted(registered - listed)}"
    assert not listed - registered, f"in AGENT.md but not registered: {sorted(listed - registered)}"


def test_the_stated_tool_count_matches_the_table():
    stated = int(re.search(r"^(\d+) tools\.", _AGENT_MD.read_text(), re.M).group(1))

    assert stated == len(_table_entries())


def test_the_warning_markers_match_the_real_gates():
    """The table's safety claims are the ones most worth keeping honest: a tool
    shown as gated that isn't is a lie about what Ada will do without asking."""
    always = {k for k, v in settings.DEFAULTS["autonomy"]["tools"].items() if v == settings.CONFIRM}

    for name, marker in _table_entries().items():
        tool = get_tool(name)
        per_call = tool is not None and tool.confirm_when is not None

        if marker == "⚠️":
            assert name in always, f"{name} is marked as always asking, but isn't gated in DEFAULTS"
        elif marker == "⚠️*":
            assert per_call, f"{name} is marked as asking per-call, but has no confirm_when predicate"
        else:
            assert name not in always, f"{name} is gated in DEFAULTS but shown as running freely"
            assert not per_call, f"{name} has a confirm_when predicate but is shown as running freely"
