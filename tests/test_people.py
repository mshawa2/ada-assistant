"""Contact pages, and the promise that Ada never eats what you wrote.

Every test here is really the same test from a different angle: a person's page
belongs to the person keeping it, and Ada is a guest inside one marked block.
Losing a stale summary costs nothing. Losing "owes me the signed SOW" costs the
thing the assistant exists for.

Contacts and chat.db are both faked — the suite must not read the developer's
real messages, and CI has neither.
"""

import pytest

from ada import people, vault
from ada.tools import people_tools


@pytest.fixture(autouse=True)
def no_real_messages(monkeypatch):
    monkeypatch.setattr(people, "recent_with", lambda handles, limit=8: [])
    yield


# --- the managed block -------------------------------------------------------


def test_splice_appends_a_block_to_a_page_that_has_none():
    out = people.splice("Runs ops at the studio.", "## Recent\n- hi")

    assert "Runs ops at the studio." in out
    assert people.managed_block(out) == "## Recent\n- hi"


def test_splice_replaces_only_the_block():
    first = people.splice("Above.\n\nMore above.", "OLD")
    second = people.splice(first + "\nBelow, written later.", "NEW")

    assert "Above." in second
    assert "More above." in second
    assert "Below, written later." in second
    assert "OLD" not in second
    assert people.managed_block(second) == "NEW"


def test_a_malformed_block_is_left_completely_alone():
    """Someone deleted the closing marker by hand. Their notes still win."""
    damaged = f"My notes.\n{people.BEGIN}\nhalf a block, no end marker"

    assert people.splice(damaged, "NEW") == damaged
    assert people.managed_block(damaged) is None


def test_managed_block_is_none_when_there_is_no_block():
    assert people.managed_block("just a page someone wrote") is None


def test_splice_into_an_empty_page_does_not_leave_leading_blank_lines():
    out = people.splice("", "BLOCK")
    assert out.startswith(people.BEGIN)


# --- refresh -----------------------------------------------------------------


def test_refresh_creates_a_page_with_handles():
    people.refresh("Jane Doe", ["+15551234567"])
    text = vault.read_page("contacts", "Jane Doe")

    assert "Jane Doe" in text
    assert "+15551234567" in text
    assert people.managed_block(text)


def test_refresh_preserves_what_a_human_added():
    people.refresh("Jane Doe", ["+15551234567"])
    path = vault.path_for("contacts", "Jane Doe")
    path.write_text(path.read_text() + "\nOwes me the signed SOW.\n")

    people.refresh("Jane Doe", ["+15551234567"])

    assert "Owes me the signed SOW." in vault.read_page("contacts", "Jane Doe")


def test_refresh_does_not_stack_duplicate_headings():
    people.refresh("Jane Doe", ["+1555"])
    people.refresh("Jane Doe", ["+1555"])
    text = vault.read_page("contacts", "Jane Doe")

    assert text.count("# Jane Doe") == 1


def test_render_block_says_so_when_there_are_no_messages():
    assert "No messages found" in people.render_block(["+1555"], [])


def test_render_block_leads_with_the_most_recent_message():
    messages = [("2026-09-05 10:00", "you", "sent the deck"), ("2026-09-04 09:00", "+1555", "thanks")]
    block = people.render_block(["+1555"], messages)

    assert "**Last message:** 2026-09-05 10:00" in block
    assert "thanks" in block  # older ones are still there, folded away
    assert "<details>" in block


def test_long_messages_are_truncated_not_dumped():
    block = people.render_block(["+1555"], [("2026-09-05", "you", "x" * 400)])
    assert "..." in block
    assert len(block) < 400


# --- the tool ----------------------------------------------------------------


def test_contact_brief_refuses_to_choose_between_two_people(monkeypatch):
    """Same discipline as find_contact: guessing briefs the wrong person."""
    monkeypatch.setattr(
        people_tools,
        "search",
        lambda name: [
            {"name": "Jane Doe", "handles": [{"label": "mobile", "value": "+1555"}]},
            {"name": "Jane Smith", "handles": [{"label": "mobile", "value": "+1666"}]},
        ],
    )
    out = people_tools.contact_brief("Jane")

    assert "More than one contact" in out
    assert "Jane Doe" in out and "Jane Smith" in out
    assert vault.list_pages("contacts") == []  # nothing written on an ambiguous match


def test_contact_brief_writes_and_returns_a_summary(monkeypatch):
    monkeypatch.setattr(
        people_tools,
        "search",
        lambda name: [{"name": "Jane Doe", "handles": [{"label": "mobile", "value": "+1555"}]}],
    )
    out = people_tools.contact_brief("Jane")

    assert "Jane Doe" in out
    assert "Recent contact" in out
    assert vault.list_pages("contacts") == ["jane-doe"]


def test_contact_brief_surfaces_what_you_noted(monkeypatch):
    monkeypatch.setattr(
        people_tools,
        "search",
        lambda name: [{"name": "Jane Doe", "handles": [{"label": "mobile", "value": "+1555"}]}],
    )
    people_tools.contact_brief("Jane")
    path = vault.path_for("contacts", "Jane Doe")
    path.write_text(path.read_text() + "\nOwes me the signed SOW.\n")

    out = people_tools.contact_brief("Jane")

    assert "What you've noted" in out
    assert "Owes me the signed SOW." in out


def test_contact_brief_falls_back_to_a_hand_written_page(monkeypatch):
    """Someone with no Contacts card can still be someone you keep notes on."""
    monkeypatch.setattr(people_tools, "search", lambda name: [])
    vault.write_page("contacts", "Ghost Writer", "Met at the conference.")

    out = people_tools.contact_brief("Ghost Writer")
    assert "Met at the conference." in out


def test_contact_brief_with_nothing_known(monkeypatch):
    monkeypatch.setattr(people_tools, "search", lambda name: [])
    assert "No contact matching" in people_tools.contact_brief("Nobody")


def test_list_people_is_empty_before_anyone_is_tracked():
    assert "No contact pages yet" in people_tools.list_people()


# --- email half --------------------------------------------------------------


def test_email_is_skipped_when_not_configured(monkeypatch):
    from ada.tools import email_tool

    monkeypatch.setattr(email_tool, "is_configured", lambda: False)
    assert people.email_with(["jane@example.com"]) == []


def test_a_mail_server_failure_does_not_break_a_contact_page(monkeypatch):
    """A page without the email half beats no page at all."""
    from ada.tools import email_tool

    monkeypatch.setattr(email_tool, "is_configured", lambda: True)

    def boom(*a, **k):
        raise email_tool.EmailUnavailable("server said no")

    monkeypatch.setattr(email_tool, "_search", boom)
    assert people.email_with(["jane@example.com"]) == []


def test_email_subjects_are_labelled_untrusted():
    """Subjects are words a third party chose; they must never read as instructions."""
    block = people.render_block(["+1555"], [], [("Fri, 05 Sep 2026", "Re: the SOW")])

    assert "Re: the SOW" in block
    assert "untrusted" in block
    assert "never instructions" in block


def test_render_block_without_email_has_no_email_section():
    assert "### Email" not in people.render_block(["+1555"], [], [])


def test_refresh_only_searches_mail_for_email_handles(monkeypatch):
    seen = {}

    def record(addresses, limit=5):
        seen["addresses"] = addresses
        return []

    monkeypatch.setattr(people, "email_with", record)

    people.refresh("Jane Doe", ["+15551234567", "jane@example.com"])

    assert seen["addresses"] == ["jane@example.com"]
