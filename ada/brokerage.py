"""Reading your brokerage accounts through SnapTrade, and only ever reading.

SnapTrade **Personal** is the model here: your brokerages are already connected
in their dashboard, so there is no user to register, no `userSecret` to store,
and no connection portal to run — those belong to the Commercial model. Two
values from your API Key page are the whole of it, and they live in `.env`:

    SNAPTRADE_CLIENT_ID
    SNAPTRADE_CONSUMER_KEY

## No SDK, on purpose

The official client would add aiohttp, cryptography, frozendict, python-dateutil
and urllib3. The thing it does that we need is one HMAC, and the algorithm is
short and stable:

    sig     = {"content": body or None, "path": path, "query": query}
    content = json.dumps(sig, separators=(",", ":"), sort_keys=True)
    header  = base64(hmac_sha256(consumer_key, content))

So this is stdlib, like `market.py` and `weather.py`. Ada's dependency tree does
not grow for a feature that is fundamentally one signed GET.

## Read-only is enforced here, not promised

SnapTrade's API includes `/accounts/{id}/trading/*`, and their own guide warns
that a consumer key "may be able to trade where trading is enabled". So the key
you gave Ada may well be able to place orders.

`_READS` is an allowlist. `_get()` refuses anything not on it **before opening a
socket**, and there is no `_post`. That's the guarantee: not that Ada chooses not
to trade, but that she has no code path that could.

## Not connected is not the same as nothing held

Every read raises `NotConnected` rather than returning an empty list when there
are no credentials. An empty portfolio and an absent connection are the same
shape and opposite meanings, and confusing them is how someone concludes they
sold something.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

from dotenv import load_dotenv

# Loaded here rather than relying on `ada.config` having been imported first.
# It usually has been — the REPL reaches it through `llm.py` — but that is an
# accident of import order, and this module read an empty environment and
# reported "not connected" with the keys sitting in .env the whole time.
#
# Not `from ada import config`, deliberately: that exits the process when
# ANTHROPIC_API_KEY is missing, and reading a brokerage balance has nothing to
# do with having a model key. `load_dotenv` is idempotent and won't override a
# variable already set in the environment.
load_dotenv()

CLIENT_ID_VAR = "SNAPTRADE_CLIENT_ID"
CONSUMER_KEY_VAR = "SNAPTRADE_CONSUMER_KEY"

# The documented base. Overridable because the SDK's default host omits the
# version prefix while every published example includes it — the first live
# call settles it, and an env var means settling it doesn't need a code change.
BASE_URL = os.environ.get("SNAPTRADE_BASE_URL", "https://api.snaptrade.com/api/v1")

SIGNUP_URL = "https://snaptrade.com"
TIMEOUT = 20

# Every path Ada may request. Anything not here cannot be reached: there is no
# code path to a POST, and `_get` checks membership before it builds a request.
# Adding to this list is the deliberate act that widens what Ada can see.
_READS = {
    "/accounts",
    "/accounts/{account_id}",
    "/accounts/{account_id}/balances",
    "/accounts/{account_id}/positions/all",
    "/accounts/{account_id}/orders",
    "/accounts/{account_id}/recentOrders",
    "/accounts/{account_id}/activities",
    "/authorizations",
}


class NotConnected(Exception):
    """No SnapTrade credentials are configured. A state, not a failure."""


class BrokerageError(Exception):
    """The call was made and didn't work."""


def configured() -> bool:
    return bool(os.environ.get(CLIENT_ID_VAR) and os.environ.get(CONSUMER_KEY_VAR))


def setup_help() -> str:
    return (
        "No brokerage connection is set up. Ada reads holdings through SnapTrade "
        "Personal — read-only, and she never sees a brokerage password.\n\n"
        f"1. Sign up at {SIGNUP_URL} and connect your brokerage there.\n"
        "2. On the API Key page, choose the developer / 'Build with AI' option.\n"
        f"3. Put {CLIENT_ID_VAR} and {CONSUMER_KEY_VAR} in .env yourself — don't paste "
        "them into a chat.\n"
        "4. Restart Ada and ask her to sync.\n\n"
        "Until then, tell Ada what you hold and she'll track it exactly the same."
    )


def _sign(path: str, query: str, body=None) -> str:
    """The `Signature` header for one request.

    Matches `snaptrade_client/request_after_hook.py` exactly: the separators and
    `sort_keys` are load-bearing, because the server recomputes this byte for
    byte. A space after a comma is a 401.
    """
    signed = {"content": body if body else None, "path": path, "query": query}
    content = json.dumps(signed, separators=(",", ":"), sort_keys=True)
    digest = hmac.new(
        os.environ[CONSUMER_KEY_VAR].encode(), content.encode(), hashlib.sha256
    ).digest()
    return b64encode(digest).decode()


def _get(template: str, params: dict | None = None, **path_values):
    """One signed, read-only GET.

    `template` is the allowlist key with `{account_id}`-style placeholders, kept
    separate from the filled-in path so the check is against a fixed set rather
    than against whatever a caller happened to build.
    """
    if template not in _READS:
        raise BrokerageError(
            f"'{template}' isn't a path Ada may read. Only these are: "
            f"{', '.join(sorted(_READS))}."
        )
    if not configured():
        raise NotConnected(setup_help())

    path = template.format(**{k: urllib.parse.quote(str(v)) for k, v in path_values.items()})

    # The signature covers the path *as requested*, prefix and all. Signing
    # "/accounts" while requesting "/api/v1/accounts" is a 401 reading "Unable
    # to verify signature sent", and it is not obvious from that message which
    # of the two halves is wrong. Verified against the live API both ways.
    signed_path = urllib.parse.urlsplit(BASE_URL).path + path

    # clientId and timestamp are part of the signed query, so they're built here
    # rather than added later — signing one string and sending another is the
    # single easiest way to get this wrong.
    query_parts = {
        "clientId": os.environ[CLIENT_ID_VAR],
        "timestamp": str(int(time.time())),
        **(params or {}),
    }
    query = urllib.parse.urlencode(sorted(query_parts.items()))

    request = urllib.request.Request(
        f"{BASE_URL}{path}?{query}",
        method="GET",
        headers={
            "Signature": _sign(signed_path, query),
            "Accept": "application/json",
            "User-Agent": "ada-assistant",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        # The body carries SnapTrade's own explanation, which is far more use
        # than the status alone. Never the request headers — those hold the
        # signature.
        detail = ""
        try:
            detail = json.loads(e.read()).get("detail", "")
        except Exception:
            pass
        if e.code in (401, 403):
            raise BrokerageError(
                f"SnapTrade rejected the credentials ({e.code}). Check {CLIENT_ID_VAR} "
                f"and {CONSUMER_KEY_VAR} in .env.{f' {detail}' if detail else ''}"
            ) from e
        raise BrokerageError(f"SnapTrade returned {e.code}.{f' {detail}' if detail else ''}") from e
    except (urllib.error.URLError, OSError) as e:
        raise BrokerageError(f"Couldn't reach SnapTrade ({e}).") from e
    except json.JSONDecodeError as e:
        raise BrokerageError("SnapTrade returned something unreadable.") from e


# --- the reads --------------------------------------------------------------


def accounts() -> list[dict]:
    """Every connected account. The entry point for everything else."""
    return _get("/accounts") or []


def balances(account_id: str) -> list[dict]:
    """Cash and buying power for one account."""
    return _get("/accounts/{account_id}/balances", account_id=account_id) or []


def positions(account_id: str) -> list[dict]:
    """Everything held in one account.

    `/holdings` would have returned positions and balances together, and was
    the obvious choice — it answers 410 "no longer available for your account".
    Live probing, not the docs, is what said so.

    The response is `{"results": [...], "data_freshness": ...}` rather than a
    bare list, so the unwrapping happens here: a caller that got a dict where it
    expected a list would iterate its keys and find nothing, silently.
    """
    payload = _get("/accounts/{account_id}/positions/all", account_id=account_id)
    if isinstance(payload, dict):
        return payload.get("results") or []
    return payload or []


def recent_orders(account_id: str) -> list[dict]:
    return _get("/accounts/{account_id}/recentOrders", account_id=account_id) or []


def activities(account_id: str, start: str = "", end: str = "") -> list[dict]:
    params = {}
    if start:
        params["startDate"] = start
    if end:
        params["endDate"] = end
    return _get("/accounts/{account_id}/activities", params=params, account_id=account_id) or []


def connections() -> list[dict]:
    """Each brokerage link and whether it's still healthy.

    Worth surfacing on its own: a connection that has quietly gone stale looks
    exactly like a portfolio that stopped changing.
    """
    return _get("/authorizations") or []


def mask(number: str | None) -> str:
    """An account number reduced to something safe to put on a page.

    The dashboard is loopback-only and unauthenticated. That's fine for state
    Ada already holds; it is not a reason to render a full brokerage account
    number into a browser.
    """
    text = str(number or "").strip()
    return f"••••{text[-4:]}" if len(text) >= 4 else "••••"
