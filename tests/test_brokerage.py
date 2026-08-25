"""Reading a brokerage through SnapTrade Personal, and only ever reading.

Two things carry the weight: the signature has to match byte for byte or every
call is a 401, and the read-only guarantee has to be a property of the code
rather than a promise about behaviour — the consumer key may itself be able to
trade.
"""

import json
import urllib.error
import urllib.request
from base64 import b64encode

import pytest

from ada import brokerage


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(brokerage.CLIENT_ID_VAR, "test-client")
    monkeypatch.setenv(brokerage.CONSUMER_KEY_VAR, "test-consumer-key")


@pytest.fixture
def http(monkeypatch):
    """Capture the request instead of sending it."""
    sent = {}

    class FakeResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["method"] = request.get_method()
        sent["headers"] = dict(request.headers)
        return FakeResponse(sent.get("payload", []))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return sent


# --- the signature ----------------------------------------------------------


def _sdk_signature(path, consumer_key, body, query):
    """SnapTrade's own algorithm, copied verbatim from the published SDK's
    `request_after_hook.compute_request_signature`. If ours and this disagree,
    ours is wrong — the server recomputes it byte for byte."""
    import hashlib
    import hmac

    sig_object = {"content": None if not body else body, "path": path, "query": query}
    sig_content = json.dumps(sig_object, separators=(",", ":"), sort_keys=True)
    return b64encode(hmac.new(consumer_key.encode(), sig_content.encode(), hashlib.sha256).digest()).decode()


def test_our_signature_matches_the_published_algorithm(keyed):
    for path, query in [
        ("/accounts", "clientId=test-client&timestamp=1700000000"),
        ("/accounts/abc-123/balances", "clientId=test-client&timestamp=1700000001"),
        ("/authorizations", "clientId=test-client&timestamp=1700000002"),
    ]:
        assert brokerage._sign(path, query) == _sdk_signature(path, "test-consumer-key", None, query)


def test_the_signed_json_has_no_incidental_whitespace(keyed, monkeypatch):
    """The separators and sort_keys are load-bearing. A space after a comma is
    a 401, and it would be a maddening one to find."""
    captured = {}

    real_dumps = json.dumps

    def spy(obj, **kwargs):
        if isinstance(obj, dict) and set(obj) == {"content", "path", "query"}:
            captured["kwargs"] = kwargs
        return real_dumps(obj, **kwargs)

    monkeypatch.setattr(json, "dumps", spy)
    brokerage._sign("/accounts", "clientId=x")

    assert captured["kwargs"]["separators"] == (",", ":")
    assert captured["kwargs"]["sort_keys"] is True


def test_the_signature_is_sent_as_a_header_and_never_in_the_url(keyed, http):
    brokerage.accounts()

    assert "Signature" in http["headers"]
    assert "Signature" not in http["url"]
    assert "test-consumer-key" not in http["url"], "the secret never travels in a query string"


def test_what_is_signed_is_what_is_sent(keyed, http):
    """Signing one string and sending another is the easiest way to get this
    wrong, and it fails as an opaque 401."""
    brokerage.accounts()

    path_and_query = http["url"].split(brokerage.BASE_URL, 1)[1]
    path, query = path_and_query.split("?", 1)

    assert http["headers"]["Signature"] == brokerage._sign(path, query)


def test_the_client_id_and_timestamp_are_in_the_signed_query(keyed, http):
    brokerage.accounts()

    assert "clientId=test-client" in http["url"]
    assert "timestamp=" in http["url"]


# --- read-only, as a property of the code -----------------------------------


def test_every_allowed_path_is_a_read():
    """The consumer key may itself be able to trade. This is the guarantee
    that Ada has no code path which could."""
    for path in brokerage._READS:
        assert "trading" not in path
        assert not any(word in path for word in ("cancel", "replace", "preview", "impact"))


def test_there_is_no_way_to_send_anything_but_a_get(keyed, http):
    brokerage.accounts()
    assert http["method"] == "GET"

    assert not hasattr(brokerage, "_post")
    assert not hasattr(brokerage, "_put")
    assert not hasattr(brokerage, "_delete")


def test_a_path_outside_the_allowlist_never_reaches_the_network(keyed, http):
    with pytest.raises(brokerage.BrokerageError, match="isn't a path Ada may read"):
        brokerage._get("/accounts/{account_id}/trading/place", account_id="abc")

    assert http == {}, "refused before a socket was opened"


def test_the_allowlist_is_checked_before_credentials_are(monkeypatch, http):
    """Order matters: a forbidden path is forbidden whether or not anyone is
    logged in, and the clearer error is the one about the path."""
    monkeypatch.delenv(brokerage.CLIENT_ID_VAR, raising=False)

    with pytest.raises(brokerage.BrokerageError, match="isn't a path"):
        brokerage._get("/accounts/{account_id}/trading/place", account_id="abc")


# --- not connected is not "nothing held" ------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: brokerage.accounts(),
        lambda: brokerage.balances("a"),
        lambda: brokerage.holdings("a"),
        lambda: brokerage.positions("a"),
        lambda: brokerage.recent_orders("a"),
        lambda: brokerage.activities("a"),
        lambda: brokerage.connections(),
    ],
)
def test_every_read_refuses_rather_than_returning_empty(monkeypatch, call):
    monkeypatch.delenv(brokerage.CLIENT_ID_VAR, raising=False)
    monkeypatch.delenv(brokerage.CONSUMER_KEY_VAR, raising=False)

    with pytest.raises(brokerage.NotConnected):
        call()


def test_the_setup_help_says_what_to_do_and_what_still_works(monkeypatch):
    monkeypatch.delenv(brokerage.CLIENT_ID_VAR, raising=False)

    help_text = brokerage.setup_help()

    assert "snaptrade.com" in help_text
    assert "never sees a brokerage password" in help_text
    assert "don't paste" in help_text.lower(), "credentials go in .env, not into a chat"
    assert "tell ada what you hold" in help_text.lower(), "manual tracking still works"


# --- failures that say something --------------------------------------------


def test_rejected_credentials_name_the_variables_to_check(keyed, monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(brokerage.BrokerageError) as caught:
        brokerage.accounts()

    assert brokerage.CLIENT_ID_VAR in str(caught.value)
    assert brokerage.CONSUMER_KEY_VAR in str(caught.value)


def test_an_unreachable_service_is_an_error_not_an_empty_portfolio(keyed, monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(brokerage.BrokerageError, match="Couldn't reach SnapTrade"):
        brokerage.accounts()


# --- what reaches a browser -------------------------------------------------


def test_account_numbers_are_masked_before_they_can_be_rendered():
    """The dashboard is loopback-only and unauthenticated. That's fine for
    state Ada already holds; it isn't a reason to paint a brokerage account
    number into a browser."""
    assert brokerage.mask("123456789012") == "••••9012"
    assert brokerage.mask("12") == "••••"
    assert brokerage.mask(None) == "••••"
