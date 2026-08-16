"""The way back when routing guessed wrong.

`ada/routing.py` trims 52 tool schemas down to the handful a turn probably
needs. *Probably* is doing work in that sentence: sooner or later it will leave
out exactly the tool required, and without a way back the answer becomes "I
can't do that" about something Ada plainly can. That's a far worse failure than
the tokens routing saves.

So this is always sent. A model that can't see what it needs asks for it, gets
the real schema, and uses it — costing one extra round trip in the rare case
rather than a refusal. It's what turns routing from a capability limit into an
optimization.
"""

import json

from ada.tools.registry import specs as all_specs
from ada.tools.registry import tool

MAX_MATCHES = 6

# Below this, a "match" is noise. Found the hard way: "pilot a helicopter"
# matched six tools purely on the word "a", which would mean this never comes
# back empty — and then "only say it's impossible if this finds nothing" is
# advice that never applies.
#
# Tuned by measurement, not taste, against real queries and junk ones:
#   1 -> found 9/9 real, but wrongly matched 3/5 junk
#   2 -> found 8/9 real, wrongly matched 0/5   <- here
#   3 -> found 7/9 real, wrongly matched 0/5
MIN_SCORE = 2

# Words that appear in almost every tool description and carry no signal.
_STOPWORDS = {
    "a", "an", "the", "to", "of", "for", "in", "on", "at", "by", "with", "and", "or",
    "is", "it", "this", "that", "use", "user", "users", "you", "your", "them", "their",
    "when", "what", "if", "do", "does", "can", "will", "be", "as", "from", "not", "no",
    "one", "any", "some", "all", "so", "than", "then", "there", "here", "about", "into",
}


def _stems(word: str) -> set:
    """A word plus crude variants, so 'spend' reaches 'spending'.

    Without this, "what did I spend" found nothing at all while get_spending
    sat right there — an escape hatch that misses a plain question is worse
    than one that's a bit noisy.
    """
    forms = {word}
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            forms.add(word[: -len(suffix)])
    return forms


def _words(text: str) -> set:
    raw = {w for w in "".join(c if c.isalnum() else " " for c in text.lower()).split() if w}
    expanded = set()
    for word in raw - _STOPWORDS:
        expanded |= _stems(word)
    return expanded


@tool(
    name="find_tool",
    description=(
        "Find a tool you can't currently see. Only some of your tools are listed on any "
        "given turn, to keep things fast — if the user asks for something and you don't "
        "have an obvious tool for it, call this with a plain description of what you need "
        "('send an email', 'turn off a light', 'what did I spend') BEFORE telling them you "
        "can't. It returns the full definitions of any matching tools, which you can then "
        "call normally on your next turn.\n\n"
        "Only say something isn't possible after this comes back empty."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "need": {
                "type": "string",
                "description": "What you're trying to do, in plain words.",
            }
        },
        "required": ["need"],
    },
)
def find_tool(need: str) -> str:
    need = (need or "").strip()
    if not need:
        raise ValueError("Say what you're trying to do.")

    asked = _words(need)
    scored = []
    for spec in all_specs():
        if spec["name"] == "find_tool":
            continue
        name_words = _words(spec["name"].replace("_", " "))
        description_words = _words(spec.get("description", ""))
        score = 3 * len(name_words & asked) + len(description_words & asked)
        if score >= MIN_SCORE:
            scored.append((score, spec))

    if not scored:
        names = ", ".join(sorted(s["name"] for s in all_specs()))
        return (
            f"No tool matches '{need}'. Everything that exists: {names}. "
            "If none of these fit, tell the user plainly that it isn't something you can do."
        )

    scored.sort(key=lambda item: -item[0])
    matches = [spec for _score, spec in scored[:MAX_MATCHES]]
    return (
        f"{len(matches)} tool(s) match '{need}'. Their full definitions follow — call one "
        "directly now that you can see it:\n\n" + json.dumps(matches, indent=2)
    )
