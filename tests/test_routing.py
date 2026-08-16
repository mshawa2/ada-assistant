"""Tool routing.

Almost every test here is about routing *not* costing a capability. The token
saving is easy; the danger is Ada saying "I can't do that" about something she
plainly can, and these pin down the three guarantees that prevent it.
"""

import json

import pytest

from ada import routing, safety, settings, store
from ada.tools import specs
from ada.tools.discovery import find_tool

ALL = specs()


def _names(msgs):
    return {s["name"] for s in routing.relevant(msgs, ALL)}


def _said(text):
    return [{"role": "user", "content": text}]


# --- it actually saves something --------------------------------------------


def test_routing_sends_fewer_tools_than_exist():
    assert len(routing.relevant(_said("hello"), ALL)) < len(ALL)


def test_the_saving_is_substantial():
    """~9k tokens of fixed overhead per call was the reason for all this."""
    full = len(json.dumps(ALL))
    routed = len(json.dumps(routing.relevant(_said("what's the weather"), ALL)))

    assert routed < full * 0.6


def test_routing_can_be_turned_off():
    store.write_json(settings._CONFIG_FILE, {"tools": {"routing": "all"}})
    settings._cache["stamp"] = None

    assert len(routing.relevant(_said("hello"), ALL)) == len(ALL)


def test_a_small_catalogue_is_sent_whole():
    """Below the limit there's nothing to gain and everything to lose."""
    few = ALL[:5]

    assert routing.relevant(_said("anything"), few) == few


# --- guarantee 1: the core is always there ----------------------------------


@pytest.mark.parametrize("message", ["hello", "turn off the lights", "", "asdfghjkl"])
def test_the_escape_hatch_is_always_sent(message):
    """Without find_tool, a routing miss becomes a refusal."""
    assert "find_tool" in _names(_said(message))


@pytest.mark.parametrize("name", sorted(routing.CORE))
def test_every_core_tool_is_always_sent(name):
    assert name in _names(_said("something unrelated entirely"))


def test_every_gated_tool_is_reachable_or_findable():
    """A confirmation-worthy action must never be silently absent: either it's
    always sent, or find_tool can produce it."""
    routed = _names(_said("hello"))
    for name in safety.load_confirmation_required_tools():
        found = name in routed or name in find_tool(name.replace("_", " "))
        assert found, f"{name} is neither routed by default nor findable"


# --- guarantee 2: relevance actually works ----------------------------------


@pytest.mark.parametrize(
    "asked,expected",
    [
        ("turn off the kitchen lights", "control_home_device"),
        ("what did I spend this month", "get_spending"),
        ("play some music", "play_music"),
        ("read me my unread email", "list_email"),
        ("text Sam I'm running late", "send_message"),
        ("what's Sam's number", "find_contact"),
        ("was the heating on last night", "device_history"),
    ],
)
def test_the_obvious_tool_is_routed_in(asked, expected):
    assert expected in _names(_said(asked))


def test_unrelated_tools_are_left_out():
    routed = _names(_said("what's on my calendar tomorrow"))

    assert "make_purchase" not in routed
    assert "system_control" not in routed


def test_context_comes_from_several_turns():
    """A follow-up like 'and the other one' has no keywords of its own."""
    conversation = _said("turn off the kitchen light") + [
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "and the other one"},
    ]

    assert "control_home_device" in _names(conversation)


def test_adas_own_words_do_not_steer_routing():
    """Her replies mention tools constantly; scoring them would pin whatever
    she last talked about permanently in scope."""
    text = routing._recent_text(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "I could check your calendar or your email"},
        ]
    )

    assert "calendar" not in text


# --- guarantee 3: whatever was used stays available -------------------------


def test_a_tool_already_used_stays_available():
    """Follow-ups must not lose the tool that produced the first answer."""
    conversation = [
        {"role": "user", "content": "check my inbox"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "list_email", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "..."}]},
        {"role": "user", "content": "what about the second one"},
    ]

    assert "list_email" in _names(conversation)


def test_used_tools_are_found_in_sdk_objects_too():
    class Block:
        type = "tool_use"
        name = "play_music"

    assert routing.used_in([{"role": "assistant", "content": [Block()]}]) == {"play_music"}


# --- the escape hatch itself ------------------------------------------------


def test_find_tool_returns_a_usable_schema():
    result = find_tool("send an email to someone")

    assert "compose_email" in result
    assert "input_schema" in result, "the model needs the real definition, not a name"


def test_find_tool_covers_things_routing_leaves_out():
    """The specific recovery path: not routed in, but findable."""
    assert "make_purchase" not in _names(_said("what have I spent"))
    assert "make_purchase" in find_tool("buy something / spend money")


def test_find_tool_is_honest_when_nothing_matches():
    result = find_tool("pilot a helicopter")

    assert "No tool matches" in result
    assert "isn't something you can do" in result


def test_find_tool_needs_something_to_look_for():
    with pytest.raises(ValueError, match="what you're trying to do"):
        find_tool("   ")


def test_find_tool_does_not_offer_itself():
    assert '"find_tool"' not in find_tool("find a tool")


@pytest.mark.parametrize(
    "need,expected",
    [
        ("what did I spend", "get_spending"),
        ("send an email", "compose_email"),
        ("turn off a light", "control_home_device"),
        ("buy groceries", "shopping_list"),
        ("play a song", "play_music"),
        ("what calendars do I have", "list_calendars"),
    ],
)
def test_find_tool_answers_plain_questions(need, expected):
    # Only tools *outside* the always-routed core need this path at all;
    # list_events is in CORE, so find_tool would never be asked for it.
    """The escape hatch missing a plain question is worse than it being a bit
    noisy — 'what did I spend' found nothing until stemming reached
    get_spending."""
    assert expected in find_tool(need)


@pytest.mark.parametrize(
    "need", ["pilot a helicopter", "translate to french", "book a flight", "order me an uber"]
)
def test_find_tool_still_says_no_to_things_ada_cannot_do(need):
    assert find_tool(need).startswith("No tool matches")


def test_common_words_alone_are_not_a_match():
    """The original bug: every description contains 'a', 'the', 'user'."""
    assert find_tool("a the of user").startswith("No tool matches")
