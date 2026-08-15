"""Persisted conversation history — and specifically the two ways it can go
wrong: SDK objects that don't serialize, and trims that break tool pairing."""

from ada import session, store
from tests.conftest import FakeBlock


def _tool_exchange(tool_id="toolu_1"):
    """A realistic assistant tool_use followed by its user tool_result."""
    return [
        {"role": "user", "content": "what's on my list?"},
        {"role": "assistant", "content": [FakeBlock("tool_use", id=tool_id, name="list_reminders", input={})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "No reminders."}]},
        {"role": "assistant", "content": [FakeBlock("text", text="Nothing on it.")]},
    ]


def test_sdk_blocks_flatten_to_plain_json():
    plain = session.to_plain(_tool_exchange())

    assert plain[1]["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "list_reminders", "input": {}}
    ]
    assert plain[3]["content"] == [{"type": "text", "text": "Nothing on it."}]


def test_round_trips_through_disk():
    messages = _tool_exchange()
    session.record(messages)

    loaded = session.load()

    assert [m["role"] for m in loaded] == ["user", "assistant", "user", "assistant"]
    assert loaded[0]["content"] == "what's on my list?"
    assert loaded[1]["content"][0]["name"] == "list_reminders"


def test_load_is_empty_on_a_first_run():
    assert session.load() == []


def test_clear_forgets_the_transcript():
    session.record([{"role": "user", "content": "hi"}])
    session.clear()
    assert session.load() == []


def test_trim_keeps_everything_under_the_cap():
    messages = [{"role": "user", "content": f"m{i}"} for i in range(5)]
    assert session.trim(messages, max_messages=10) == messages


def test_trim_never_starts_on_a_tool_result():
    """Cutting between a tool_use and its tool_result hands the API a dangling
    call and it rejects the whole request — the failure this guards."""
    messages = [{"role": "user", "content": "filler"}] * 3 + _tool_exchange()

    for cap in range(1, len(messages) + 1):
        trimmed = session.trim(messages, max_messages=cap)
        if not trimmed:
            continue
        first = trimmed[0]
        assert first["role"] == "user"
        if isinstance(first["content"], list):
            types = [b.get("type") for b in first["content"] if isinstance(b, dict)]
            assert "tool_result" not in types, f"cap={cap} cut mid tool-call pair"


def test_trim_returns_the_recent_tail():
    messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    trimmed = session.trim(messages, max_messages=4)

    assert len(trimmed) <= 4
    assert trimmed[-1]["content"] == "m9"


def test_record_trims_the_callers_live_list_in_place():
    """A copy-trim would cap the file while letting in-memory context grow."""
    messages = [{"role": "user", "content": f"m{i}"} for i in range(session.MAX_MESSAGES + 10)]
    session.record(messages)

    assert len(messages) <= session.MAX_MESSAGES


def test_load_skips_malformed_entries():
    store.write_json(
        session._STORE,
        [{"role": "user", "content": "good"}, {"nonsense": True}, "not a dict"],
    )

    assert session.load() == [{"role": "user", "content": "good"}]


def test_resumed_summary_is_silent_with_nothing_to_resume():
    assert session.resumed_summary([]) is None


def test_resumed_summary_counts_real_exchanges():
    summary = session.resumed_summary(_tool_exchange())
    assert summary is not None
    assert "1 exchange" in summary
