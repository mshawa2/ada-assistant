"""Retrying a rate-limited price service, and knowing when to stop.

Two failures are being defended against, and they pull in opposite directions.

Retrying too little means one 429 silently skips a stop check — the price was
never read, so no rule was evaluated, and nothing says so out loud.

Retrying too much is worse in a different way. `positions_check` quotes every
managed position in one pass on the heartbeat's single thread; three attempts
each across fifty symbols during a real outage is minutes of sleeping, and a
reminder due in that window fires late. Hence the breaker.

`urlopen` and `sleep` are both faked: no network, no waiting.
"""

import json
import urllib.error

import pytest

from ada import market


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("http://x", code, "boom", headers, None)


@pytest.fixture(autouse=True)
def reset_breaker(monkeypatch):
    market._breaker["failures"] = 0
    market._breaker["opened_at"] = 0.0
    monkeypatch.setattr(market.time, "sleep", lambda _s: None)
    yield


def responder(monkeypatch, *outcomes):
    """Replay `outcomes` in order; each is an exception to raise or a payload."""
    calls = []

    def fake_urlopen(request, timeout=None):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(request.full_url)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(market.urllib.request, "urlopen", fake_urlopen)
    return calls


# --- retrying ----------------------------------------------------------------


def test_a_rate_limit_is_retried_and_can_succeed(monkeypatch):
    calls = responder(monkeypatch, http_error(429), {"ok": True})

    assert market._get("http://x") == {"ok": True}
    assert len(calls) == 2


def test_it_gives_up_after_three_attempts(monkeypatch):
    calls = responder(monkeypatch, http_error(429))

    with pytest.raises(market.MarketError):
        market._get("http://x")
    assert len(calls) == market._ATTEMPTS


def test_a_server_error_is_transient_too(monkeypatch):
    responder(monkeypatch, http_error(503), {"ok": True})
    assert market._get("http://x") == {"ok": True}


def test_a_network_failure_is_retried(monkeypatch):
    responder(monkeypatch, urllib.error.URLError("no route"), {"ok": True})
    assert market._get("http://x") == {"ok": True}


def test_an_unreadable_body_is_retried(monkeypatch):
    """Rate-limit pages arrive as HTML with a 200, which json refuses."""

    class BadResponse(FakeResponse):
        def read(self):
            return b"<html>slow down</html>"

    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        return BadResponse({}) if len(calls) == 1 else FakeResponse({"ok": True})

    monkeypatch.setattr(market.urllib.request, "urlopen", fake_urlopen)
    assert market._get("http://x") == {"ok": True}


# --- what must NOT be retried ------------------------------------------------


def test_an_unknown_symbol_is_not_retried(monkeypatch):
    """It won't start existing, and it isn't the service's fault."""
    calls = responder(monkeypatch, http_error(404))

    with pytest.raises(market.MarketError, match="no such symbol"):
        market._get("http://x")
    assert len(calls) == 1
    assert market._breaker["failures"] == 0  # must not count against the service


def test_a_client_error_is_not_retried(monkeypatch):
    calls = responder(monkeypatch, http_error(401))

    with pytest.raises(market.MarketError):
        market._get("http://x")
    assert len(calls) == 1


# --- the breaker -------------------------------------------------------------


def test_repeated_outages_open_the_breaker(monkeypatch):
    responder(monkeypatch, http_error(429))

    for _ in range(market._BREAKER_TRIP):
        with pytest.raises(market.MarketError):
            market._get("http://x")

    assert market._breaker_open()


def test_an_open_breaker_fails_fast_without_calling_out(monkeypatch):
    market._breaker["failures"] = market._BREAKER_TRIP
    market._breaker["opened_at"] = market.time.time()
    calls = responder(monkeypatch, {"ok": True})

    with pytest.raises(market.MarketError, match="rate-limiting"):
        market._get("http://x")
    assert calls == []  # the whole point: no request was made


def test_the_breaker_reopens_after_the_cooldown(monkeypatch):
    market._breaker["failures"] = market._BREAKER_TRIP
    market._breaker["opened_at"] = market.time.time() - market._BREAKER_COOLDOWN - 1
    responder(monkeypatch, {"ok": True})

    assert market._get("http://x") == {"ok": True}


def test_one_good_read_clears_the_record(monkeypatch):
    responder(monkeypatch, http_error(429), {"ok": True})
    market._get("http://x")

    assert market._breaker["failures"] == 0


# --- Retry-After -------------------------------------------------------------


def test_a_short_retry_after_is_honoured(monkeypatch):
    slept = []
    monkeypatch.setattr(market.time, "sleep", lambda s: slept.append(s))
    responder(monkeypatch, http_error(429, retry_after=2), {"ok": True})

    market._get("http://x")
    assert slept == [2.0]


def test_an_absurd_retry_after_is_ignored_for_the_default(monkeypatch):
    """A heartbeat check cannot sit out a five-minute wait the server suggests."""
    slept = []
    monkeypatch.setattr(market.time, "sleep", lambda s: slept.append(s))
    responder(monkeypatch, http_error(429, retry_after=300), {"ok": True})

    market._get("http://x")
    assert slept == [market._BACKOFF[0]]


def test_backoff_lengthens_between_attempts(monkeypatch):
    slept = []
    monkeypatch.setattr(market.time, "sleep", lambda s: slept.append(s))
    responder(monkeypatch, http_error(429))

    with pytest.raises(market.MarketError):
        market._get("http://x")
    assert slept == list(market._BACKOFF)
    assert slept[1] > slept[0]
