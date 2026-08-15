"""The shopping list.

No real Reminders data is touched: the EventKit store is replaced with a fake
that speaks the handful of calls this tool makes.
"""

import sys

import pytest

from ada import agent, safety, settings, store
from ada.tools import shopping


class FakeReminder:
    def __init__(self, title, calendar=None):
        self._title = title
        self._calendar = calendar
        self.completed = False

    def title(self):
        return self._title

    def setTitle_(self, value):
        self._title = value

    def setCalendar_(self, value):
        self._calendar = value

    def setCompleted_(self, value):
        self.completed = bool(value)


class FakeList:
    def __init__(self, title, writable=True):
        self._title = title
        self._writable = writable

    def title(self):
        return self._title

    def allowsContentModifications(self):
        return self._writable


class FakeStore:
    def __init__(self, lists, items):
        self.lists = lists
        self.items = items
        self.saved = []

    def calendarsForEntityType_(self, _entity):
        return self.lists

    def predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(self, _s, _e, calendars):
        return calendars

    def fetchRemindersMatchingPredicate_completion_(self, _predicate, completion):
        completion([r for r in self.items if not r.completed])

    def saveReminder_commit_error_(self, reminder, _commit, _error):
        self.saved.append(reminder)
        if reminder not in self.items:
            self.items.append(reminder)
        return True, None


class FakeEventKit:
    EKEntityTypeReminder = 1

    class EKReminder:
        @staticmethod
        def reminderWithEventStore_(_store):
            return FakeReminder("")


@pytest.fixture
def shopping_store(monkeypatch):
    """A fake Reminders backend with a 'Shopping' list. Returns the store."""
    items = [FakeReminder("milk"), FakeReminder("bread")]
    fake = FakeStore([FakeList("Shopping"), FakeList("Reminders")], items)

    monkeypatch.setattr(shopping, "_reminder_store", lambda: fake)
    monkeypatch.setitem(sys.modules, "EventKit", FakeEventKit)
    store.write_json(settings._CONFIG_FILE, {"shopping": {"list_name": "Shopping"}})
    settings._cache["stamp"] = None
    return fake


# --- reading ----------------------------------------------------------------


def test_listing_shows_what_is_on_it(shopping_store):
    result = shopping.shopping_list("list")

    assert "milk" in result and "bread" in result
    assert "2 items" in result


def test_listing_is_the_default_action(shopping_store):
    """A bare call must not change anything."""
    result = shopping.shopping_list()

    assert "milk" in result
    assert shopping_store.saved == []


def test_an_empty_list_says_so(shopping_store):
    shopping_store.items.clear()

    assert "is empty" in shopping.shopping_list("list")


def test_completed_items_are_not_shown(shopping_store):
    shopping_store.items[0].completed = True

    result = shopping.shopping_list("list")

    assert "milk" not in result
    assert "bread" in result


# --- adding -----------------------------------------------------------------


def test_adding_puts_items_on_the_list(shopping_store):
    result = shopping.shopping_list("add", ["coffee", "eggs"])

    titles = [r.title() for r in shopping_store.items]
    assert "coffee" in titles and "eggs" in titles
    assert "Added coffee, eggs" in result


def test_adding_several_items_is_one_call(shopping_store):
    shopping.shopping_list("add", ["a", "b", "c"])

    assert len(shopping_store.saved) == 3, "one call, three saves — not three calls"


def test_a_duplicate_is_reported_rather_than_added_twice(shopping_store):
    result = shopping.shopping_list("add", ["milk", "coffee"])

    assert [r.title() for r in shopping_store.items].count("milk") == 1
    assert "Already on it: milk" in result
    assert "Added coffee" in result


def test_duplicate_matching_ignores_case(shopping_store):
    result = shopping.shopping_list("add", ["MILK"])

    assert "Already on it" in result


def test_a_comma_separated_string_is_accepted(shopping_store):
    shopping.shopping_list("add", "coffee, eggs")

    titles = [r.title() for r in shopping_store.items]
    assert "coffee" in titles and "eggs" in titles


def test_adding_nothing_is_refused(shopping_store):
    with pytest.raises(ValueError, match="Nothing to add"):
        shopping.shopping_list("add", [])

    assert shopping_store.saved == []


# --- removing ---------------------------------------------------------------


def test_removing_ticks_an_item_off(shopping_store):
    result = shopping.shopping_list("remove", ["milk"])

    milk = next(r for r in shopping_store.items if r.title() == "milk")
    assert milk.completed is True
    assert "Ticked off milk" in result


def test_removing_completes_rather_than_deletes(shopping_store):
    """Ticking off is what a shopping list does, and it stays recoverable."""
    shopping.shopping_list("remove", ["milk"])

    assert len(shopping_store.items) == 2, "nothing was destroyed"


def test_removing_matches_loosely(shopping_store):
    shopping_store.items.append(FakeReminder("semi-skimmed milk"))

    result = shopping.shopping_list("remove", ["semi"])

    assert "semi-skimmed milk" in result


def test_something_not_on_the_list_is_reported(shopping_store):
    result = shopping.shopping_list("remove", ["caviar"])

    assert "Not on the list: caviar" in result


def test_removing_nothing_is_refused(shopping_store):
    with pytest.raises(ValueError, match="Nothing to remove"):
        shopping.shopping_list("remove", [])


# --- clearing ---------------------------------------------------------------


def test_clearing_ticks_everything_off(shopping_store):
    result = shopping.shopping_list("clear")

    assert all(r.completed for r in shopping_store.items)
    assert "Cleared 2 items" in result


def test_clearing_an_empty_list_says_so(shopping_store):
    shopping_store.items.clear()

    assert "already empty" in shopping.shopping_list("clear")


def test_only_clearing_asks_first():
    """The one destructive action, via the same per-call mechanism that tells
    a lock from a lamp."""
    assert agent._tool_asks_to_confirm("shopping_list", {"action": "clear"})

    for harmless in ("list", "add", "remove"):
        assert not agent._tool_asks_to_confirm("shopping_list", {"action": harmless}), harmless


def test_the_clear_confirmation_names_the_list():
    description = agent._describe_action("shopping_list", {"action": "clear"})

    assert "CLEAR" in description
    assert "Shopping" in description


def test_the_tool_is_not_blanket_gated():
    """Adding milk shouldn't need a y/n — only clearing does."""
    assert not safety.requires_confirmation("shopping_list")


# --- setup ------------------------------------------------------------------


def test_a_missing_list_names_the_ones_that_exist(shopping_store):
    store.write_json(settings._CONFIG_FILE, {"shopping": {"list_name": "Groceries"}})
    settings._cache["stamp"] = None

    with pytest.raises(shopping.ShoppingUnavailable, match="Reminders"):
        shopping.shopping_list("list")


def test_a_missing_list_is_not_silently_swapped_for_the_default(shopping_store):
    """Scattering shopping items through someone's general reminders is worse
    than one clear message."""
    store.write_json(settings._CONFIG_FILE, {"shopping": {"list_name": "Groceries"}})
    settings._cache["stamp"] = None

    with pytest.raises(shopping.ShoppingUnavailable):
        shopping.shopping_list("add", ["milk"])

    assert shopping_store.saved == []


def test_a_read_only_list_is_refused(shopping_store):
    shopping_store.lists = [FakeList("Shopping", writable=False)]

    with pytest.raises(shopping.ShoppingUnavailable, match="read-only"):
        shopping.shopping_list("list")


def test_an_unknown_action_is_refused(shopping_store):
    with pytest.raises(ValueError, match="isn't a shopping list action"):
        shopping.shopping_list("obliterate")


def test_a_missing_permission_explains_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(shopping, "_store", None)
    monkeypatch.setattr(shopping, "_denied_reason", shopping._PERMISSION_HELP)

    with pytest.raises(shopping.ShoppingUnavailable, match="System Settings"):
        shopping._reminder_store()


def test_shopping_is_separate_from_timed_reminders():
    """Same word, different thing — merging them would make both worse."""
    from ada.tools import specs

    names = {s["name"] for s in specs()}
    assert {"shopping_list", "add_reminder"} <= names


def test_the_fake_store_mirrors_the_real_eventkit_method_names():
    """A fake answers to whatever name the code invents, so it will happily
    hide a typo'd selector — which is exactly what happened here: the code
    called ...RemindersInDueDateStarting... and the real API is
    ...RemindersWithDueDateStarting.... This checks the fake's surface against
    the real class when EventKit is importable, so the fake can't drift into
    validating a method that doesn't exist.
    """
    EventKit = pytest.importorskip("EventKit")

    real = set(dir(EventKit.EKEventStore))
    faked = {name for name in dir(FakeStore) if not name.startswith("_")}

    unknown = {name for name in faked if name not in real}
    assert not unknown, f"the fake defines methods EKEventStore doesn't have: {unknown}"
