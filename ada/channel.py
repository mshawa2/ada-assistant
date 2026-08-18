"""Whether this process's replies are spoken aloud or read on a screen.

The same agent and the same system prompt drive all three front-ends, but the
two channels want genuinely different writing. A terminal reader skims a
bulleted list and clicks a link; a speech synthesiser reads "asterisk asterisk"
out loud and spells a URL out character by character. Without knowing which it
is, the prompt has to write for one and be wrong for the other.

Runtime state rather than a setting: it's a fact about how the process was
launched, not something the user configures. It's also fixed for the life of
the process, which is what lets it sit above the prompt cache breakpoint
without costing anything — see ada/llm.py.
"""

_spoken = False


def use_voice() -> None:
    """Declared by a front-end that reads its replies out loud."""
    global _spoken
    _spoken = True


def is_spoken() -> bool:
    return _spoken
