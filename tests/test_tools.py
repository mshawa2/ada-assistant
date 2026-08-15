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
