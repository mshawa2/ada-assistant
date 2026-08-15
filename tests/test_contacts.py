"""Contact lookup — mostly a test that it never decides who to text.

No real contact is read: `_all_contacts` is replaced with fakes, so the
address book on this machine is never touched.
"""

import subprocess

import pytest

from ada import agent
from ada.tools import contacts, imessage


class FakeValue:
    def __init__(self, value):
        self._value = value

    def stringValue(self):
        return self._value

    def __str__(self):
        return self._value


class FakeLabeled:
    def __init__(self, label, value):
        self._label = label
        self._value = FakeValue(value)

    def label(self):
        return self._label

    def value(self):
        return self._value


class FakeContact:
    def __init__(self, given="", family="", org="", phones=(), emails=()):
        self._given = given
        self._family = family
        self._org = org
        self._phones = [FakeLabeled(label, value) for label, value in phones]
        self._emails = [FakeLabeled(label, value) for label, value in emails]

    def givenName(self):
        return self._given

    def familyName(self):
        return self._family

    def organizationName(self):
        return self._org

    def phoneNumbers(self):
        return self._phones

    def emailAddresses(self):
        return self._emails


@pytest.fixture
def book(monkeypatch):
    """Install a fake address book. Returns the list so tests can shape it."""
    people = []
    monkeypatch.setattr(contacts, "_all_contacts", lambda: people)
    # _label_for imports Contacts for localisation; keep that out of the tests.
    monkeypatch.setattr(contacts, "_label_for", lambda labeled: labeled.label())
    return people


# --- finding the right person -----------------------------------------------


def test_a_single_match_with_one_number_is_unambiguous(book):
    book.append(FakeContact("Sam", "Reed", phones=[("mobile", "+14155550123")]))

    result = contacts.find_contact("Sam")

    assert "Sam Reed" in result
    assert "+14155550123" in result
    assert "ask the user which" not in result


def test_matching_is_case_insensitive_and_matches_any_name_part(book):
    book.append(FakeContact("Samantha", "Okafor", phones=[("mobile", "+14155550124")]))

    assert "Samantha Okafor" in contacts.find_contact("okafor")
    assert "Samantha Okafor" in contacts.find_contact("SAMANTHA")


def test_exact_and_prefix_matches_rank_above_substring_ones(book):
    book.append(FakeContact("Rosam", "Ali", phones=[("mobile", "+1")]))
    book.append(FakeContact("Sam", "Reed", phones=[("mobile", "+2")]))
    book.append(FakeContact("Samantha", "Okafor", phones=[("mobile", "+3")]))

    names = [m["name"] for m in contacts.search("sam")]

    assert names[0] == "Sam Reed"
    assert "Rosam Ali" in names, "a weaker match is still returned, not hidden"


def test_a_contact_with_no_handles_is_left_out(book):
    """They can't be texted, so offering them is just a dead end."""
    book.append(FakeContact("Ghost", "Nomobile"))

    assert contacts.search("Ghost") == []


def test_an_organisation_name_is_used_when_there_is_no_person_name(book):
    book.append(FakeContact(org="Dentist Surgery", phones=[("main", "+14155550199")]))

    assert "Dentist Surgery" in contacts.find_contact("dentist")


def test_no_match_says_so_and_tells_the_model_to_ask(book):
    result = contacts.find_contact("Nobody")

    assert "No contact matching" in result
    assert "Ask the user" in result


def test_an_empty_search_returns_nothing_rather_than_everything(book):
    book.append(FakeContact("Sam", "Reed", phones=[("mobile", "+1")]))

    assert contacts.search("   ") == []


# --- ambiguity is surfaced, never resolved ----------------------------------


def test_one_person_with_two_numbers_is_flagged_as_ambiguous(book):
    """The real 'Mom' on the test machine has exactly this shape — picking one
    would be a coin flip on which device gets read."""
    book.append(FakeContact("Mom", phones=[("mobile", "+14155550111"), ("home", "+14155550222")]))

    result = contacts.find_contact("Mom")

    assert "+14155550111" in result
    assert "+14155550222" in result
    assert "mobile" in result and "home" in result, "labels are what make the choice meaningful"
    assert "ask the user which before sending" in result


def test_two_people_with_the_same_name_are_both_returned_and_flagged(book):
    book.append(FakeContact("Sam", "Reed", phones=[("mobile", "+14155550123")]))
    book.append(FakeContact("Sam", "Okafor", phones=[("mobile", "+14155550124")]))

    result = contacts.find_contact("Sam")

    assert "Sam Reed" in result and "Sam Okafor" in result
    assert "ask the user which before sending" in result


def test_emails_are_offered_too_and_labelled_as_emails(book):
    book.append(FakeContact("Sam", "Reed", emails=[("work", "sam@example.com")]))

    result = contacts.find_contact("Sam")

    assert "sam@example.com" in result
    assert "work email" in result


def test_a_huge_number_of_matches_is_capped(book):
    for i in range(contacts.MAX_RESULTS + 5):
        book.append(FakeContact("Sam", f"Number{i}", phones=[("mobile", f"+1415555{i:04d}")]))

    result = contacts.find_contact("Sam")

    assert "more match" in result, "the overflow is reported rather than silently dropped"
    assert result.count("mobile:") == contacts.MAX_RESULTS


# --- it cannot send ---------------------------------------------------------


def test_contacts_exposes_lookup_only():
    """No write path, and nothing that sends — this tool only ever answers
    'what number does this name have'."""
    from ada.tools import specs

    names = {spec["name"] for spec in specs()}
    assert "find_contact" in names
    assert {n for n in names if "contact" in n} == {"find_contact"}


def test_a_missing_permission_explains_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(contacts, "_store", None)
    monkeypatch.setattr(contacts, "_denied_reason", contacts._PERMISSION_HELP)

    with pytest.raises(contacts.ContactsUnavailable, match="System Settings"):
        contacts._contact_store()


# --- the name reaches the confirmation prompt -------------------------------


def test_the_confirmation_shows_who_a_looked_up_number_belongs_to():
    """A bare +1773... is unrecognisable, so a wrong lookup would sail through.
    'Mom (+1773...)' is checkable at a glance."""
    description = agent._describe_action(
        "send_message",
        {"to": "+14155550111", "text": "on my way", "recipient_label": "Mom (mobile)"},
    )

    assert "Mom (mobile)" in description
    assert "+14155550111" in description


def test_the_confirmation_still_works_without_a_label():
    description = agent._describe_action("send_message", {"to": "+14155550111", "text": "hi"})

    assert "+14155550111" in description
    assert "SEND A MESSAGE" in description


def test_the_label_cannot_change_who_receives_the_message(monkeypatch):
    """It's display only — a wrong or malicious label must not misdirect."""
    captured = []
    monkeypatch.setattr(
        imessage.subprocess,
        "run",
        lambda argv, **kw: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )

    imessage.send_message("+14155550111", "hi", recipient_label="Someone Else Entirely")

    assert captured[0][3] == "+14155550111"
