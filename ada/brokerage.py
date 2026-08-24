"""Syncing holdings from a real brokerage, without holding the keys to it.

Robinhood has no public API. The unofficial clients work by logging in with the
user's password and MFA code, which means storing brokerage credentials on disk,
breaking whenever the private API moves, and violating the terms the user
agreed to — a combination that isn't worth a convenience.

**SnapTrade** is the sanctioned route: the user authorises read-only access
through Robinhood's own OAuth, and Ada never sees a password. Two keys go in
`.env`; the link step happens in a browser, once.

This module is the seam. It is deliberately explicit about being unconfigured
rather than silently returning nothing, because "no positions" and "I was never
connected" look identical in a list and mean completely different things.
"""

import os

CLIENT_ID_VAR = "SNAPTRADE_CLIENT_ID"
CONSUMER_KEY_VAR = "SNAPTRADE_CONSUMER_KEY"
USER_ID_VAR = "SNAPTRADE_USER_ID"
USER_SECRET_VAR = "SNAPTRADE_USER_SECRET"

SIGNUP_URL = "https://snaptrade.com"


class NotConnected(Exception):
    """No brokerage link is configured. Not an error — a state."""


def configured() -> bool:
    return bool(os.environ.get(CLIENT_ID_VAR) and os.environ.get(CONSUMER_KEY_VAR))


def linked() -> bool:
    """Whether a *user* has been registered and linked, not just the app keys."""
    return configured() and bool(os.environ.get(USER_ID_VAR) and os.environ.get(USER_SECRET_VAR))


def setup_help() -> str:
    """What's actually missing, said in the order it has to be done."""
    if not configured():
        return (
            "No brokerage connection is set up. Ada reads holdings through SnapTrade, which "
            f"uses Robinhood's own read-only authorisation — she never sees a password.\n\n"
            f"1. Sign up at {SIGNUP_URL} and get a client id and consumer key.\n"
            f"2. Put them in .env as {CLIENT_ID_VAR} and {CONSUMER_KEY_VAR}.\n"
            "3. Restart Ada and ask to connect the brokerage — she'll give you a link to "
            "authorise in the browser.\n\n"
            "Until then, tell Ada what you hold and she'll track it just the same."
        )
    return (
        "SnapTrade keys are configured, but no account has been linked yet. Ask Ada to connect "
        "the brokerage and authorise in the browser."
    )


def positions() -> list[dict]:
    """Holdings from the linked brokerage.

    Raises rather than returning an empty list when nothing is connected: an
    empty portfolio and an absent connection are the same shape and opposite
    meanings, and confusing them is how someone concludes they sold something.
    """
    if not linked():
        raise NotConnected(setup_help())

    # The HTTP client lands with the link flow. Raising here rather than
    # returning [] keeps the same rule: never let "not connected" look like
    # "nothing held".
    raise NotConnected(
        "Brokerage sync isn't built yet — the keys are set, but the link flow and the "
        "read call still need writing. Tell Ada what you hold in the meantime."
    )
