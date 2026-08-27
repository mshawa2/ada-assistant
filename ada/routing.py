"""Sending the model the tools it might need, not all of them.

52 tools is ~7,700 tokens of schema on *every* call, plus ~1,300 for the
system prompt. That's ~9,000 tokens of fixed overhead before a word of
conversation — around $49/month at thirty turns a day, and enough on its own
to trip the $5/day model budget. It also makes tool choice worse: picking one
of 52 near-neighbours is harder than picking one of a dozen.

**Routing is an optimization, never a capability limit.** That's the rule
everything here follows, because the failure mode of getting it wrong — Ada
saying "I can't do that" about something she plainly can — is far worse than
the tokens it saves. Three things guarantee it:

1. A **core set** is always sent: the things people ask for constantly, plus
   every gated tool, so a confirmation-worthy action is never quietly absent.
2. Anything **already used in this conversation** stays available. Follow-ups
   ("no, the other one") must not lose the tool that produced the first answer.
3. `find_tool` is always sent, so a model that needs something it can't see can
   ask for it and get the real schema back. Worst case is one extra round trip,
   not a refusal.

The scoring is lexical rather than embedding-based. Jarvis uses a local
embedding model for this; Ada has none, and adding one for a single-user
assistant is a dependency and a model download for a job plain word overlap
does adequately — *given* that the guarantees above make a miss recoverable
rather than fatal. If it ever proves not good enough, the seam to swap in
embeddings is `_score`.
"""

import re

from ada import settings

# Always sent. Constant daily use, plus every gated tool: a confirmation-worthy
# action must never be missing because a router had an opinion.
CORE = {
    "find_tool",
    "get_settings",
    "set_setting",
    "set_mode",
    "list_reminders",
    "add_reminder",
    "complete_reminder",
    "list_events",
    "shopping_list",
    "remember",
    "list_memory",
    "forget",
    "web_search",
    "fetch_url",
    "get_weather",
    "list_notices",
    "list_jobs",
    "list_schedules",
    "resolve_approval",
}

# Words that mean a tool without sharing its vocabulary. Deliberately small —
# a nudge, not an ontology, and the escape hatch covers what it misses.
_HINTS = {
    "calendar": ["free", "busy", "schedule", "meeting", "appointment", "booked", "diary"],
    "event": ["free", "busy", "schedule", "meeting", "appointment"],
    "email": ["inbox", "mail", "unread", "reply", "sent"],
    "message": ["text", "imessage", "sms", "reply"],
    "contact": ["number", "phone", "address"],
    "music": ["play", "song", "track", "album", "playlist", "listen"],
    "media": ["pause", "skip", "playing", "song"],
    "home": ["light", "lights", "lamp", "heating", "thermostat", "lock", "door", "switch"],
    "device": ["light", "lights", "lamp", "heating", "lock", "door", "plug"],
    "note": ["wrote", "written", "jot", "document"],
    "purchase": ["buy", "order", "spend", "cost", "pay"],
    "vendor": ["buy", "order", "spend"],
    "spending": ["cost", "budget", "spent", "money"],
    "shopping": ["groceries", "buy", "milk"],
    "weather": ["rain", "sunny", "cold", "hot", "forecast", "temperature"],
    "brief": ["morning", "today", "day", "catch", "summary", "overview", "briefing", "agenda"],
    "heartbeat": ["quiet", "proactive", "background"],
    "job": ["background", "queue", "queued", "later", "progress", "underway", "still"],
    "approval": ["approve", "approval", "blocked", "permission"],
    "schedule": ["recurring", "repeat", "daily", "weekly", "automatically", "morning", "routine"],
    "position": ["stock", "shares", "portfolio", "holding", "bought", "sold", "ticker", "invested"],
    "rule": ["stop", "protect", "where", "level", "exposed", "unprotected", "risk"],
    "portfolio": ["sync", "brokerage", "robinhood", "account", "cash", "balance", "holdings"],
    "accounts": ["brokerage", "robinhood", "cash", "buying", "balance"],
    "price": ["stock", "ticker", "shares", "trading", "market", "quote"],
    "analyze": ["stock", "ticker", "buy", "sell", "opinion", "thoughts", "worth"],
    "scan": ["market", "moving", "ideas", "opportunities", "news", "happening"],
}

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set:
    """Lowercase words, with a crude plural strip so 'lights' hits 'light'."""
    found = set()
    for word in _WORD.findall((text or "").lower()):
        found.add(word)
        if len(word) > 3 and word.endswith("s"):
            found.add(word[:-1])
    return found


def _recent_text(messages: list, turns: int = 4) -> str:
    """The last few turns of what the *user* said.

    Only user text is scored. Ada's own replies mention tools constantly
    ("I'll check your calendar"), which would keep whatever she last talked
    about permanently in scope.
    """
    parts = []
    for message in messages[-turns * 2 :]:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str):
            parts.append(content)
    return " ".join(parts)


def used_in(messages: list) -> set:
    """Tools already called in this conversation — kept for follow-ups."""
    names = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if kind == "tool_use":
                name = block.get("name") if isinstance(block, dict) else getattr(block, "name", None)
                if name:
                    names.add(name)
    return names


# find_tool answers a match by returning the tools' full JSON definitions, so
# every tool it offered appears as a `"name": "..."` field. Its no-match branch
# is prose listing every tool name bare, which this deliberately doesn't match
# — "here is everything that exists" must not drag all 53 back into scope.
_OFFERED_NAME = re.compile(r'"name"\s*:\s*"([a-z_]+)"')


def offered_in(messages: list, valid: set) -> set:
    """Tools that find_tool has handed the model the definition of.

    Guarantee 3 in the module docstring — the escape hatch — only holds if
    these come back in the tool list. Without it the model gets the schema it
    asked for, still can't call the tool, and tells the user it can't do
    something it plainly can: the exact failure routing is supposed to make
    impossible.

    This is the one thing that can change the tool set mid-turn, which costs a
    prompt-cache write (see ada/llm.py). It only happens on a turn where
    routing already guessed wrong, and a cache miss is a far better outcome
    than a false refusal.
    """
    names = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = block.get("content")
            if isinstance(text, str):
                names |= set(_OFFERED_NAME.findall(text)) & valid
    return names


def _score(spec: dict, asked: set) -> int:
    """How much this tool looks like what the user just said."""
    name_words = _words(spec["name"].replace("_", " "))
    description_words = _words(spec.get("description", ""))

    # The name is a far stronger signal than the description, which is long
    # prose full of common words.
    score = 3 * len(name_words & asked) + len(description_words & asked)

    for key, synonyms in _HINTS.items():
        if key in name_words and asked.intersection(synonyms):
            score += 3
    return score


def relevant(messages: list, all_specs: list) -> list:
    """The tools worth sending for this turn."""
    if (settings.get("tools.routing", "auto") or "auto").lower() != "auto":
        return all_specs

    limit = int(settings.get("tools.max", 22) or 22)
    if len(all_specs) <= limit:
        return all_specs

    valid = {spec["name"] for spec in all_specs}
    keep = set(CORE) | used_in(messages) | offered_in(messages, valid)
    asked = _words(_recent_text(messages))

    scored = sorted(
        ((_score(spec, asked), spec["name"]) for spec in all_specs if spec["name"] not in keep),
        reverse=True,
    )
    for score, name in scored:
        if len(keep) >= limit:
            break
        if score > 0:
            keep.add(name)

    chosen = [spec for spec in all_specs if spec["name"] in keep]
    # Never return nothing useful, whatever the config says.
    return chosen or all_specs
