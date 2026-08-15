"""Spending money.

Nothing here reaches the network: `urlopen` is replaced in every test. Tokens
and vendor URLs are synthetic. Most of these tests are about the things that
*stop* a purchase, because that's where this tool's value is.
"""

import io
import urllib.error

import pytest

from ada import agent, budget, safety, settings, store
from ada.tools import purchasing
from tests.conftest import FakeBlock, FakeMessage, fake_stream_turn

OPEN_BUDGET = {"per_day": 500.0, "per_month": 500.0}


def _configure(monkeypatch, vendors=None, purchase_budget=None, token="synthetic-token"):
    """Open some of the five gates. Anything not passed stays closed."""
    vendors = vendors if vendors is not None else {
        "groceries": {
            "url": "https://synthetic.example/buy",
            "description": "Adds to my shopping basket",
            "max_amount": 100.0,
        }
    }
    config = {"purchasing": {"vendors": vendors}}
    if purchase_budget is not None:
        config["budget"] = {"limits": {"purchases": purchase_budget}}
    store.write_json(settings._CONFIG_FILE, config)
    settings._cache["stamp"] = None

    for name in vendors:
        if token:
            monkeypatch.setenv(purchasing._token_name(name), token)
        else:
            monkeypatch.delenv(purchasing._token_name(name), raising=False)


@pytest.fixture
def sent(monkeypatch):
    """Capture the outbound request instead of making it."""
    calls = []

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return FakeResponse(b"ok")

    monkeypatch.setattr(purchasing.urllib.request, "urlopen", fake_urlopen)
    return calls


# --- everything defaults closed ---------------------------------------------


def test_no_vendors_ship_with_ada():
    assert settings.DEFAULTS["purchasing"]["vendors"] == {}


def test_the_purchase_budget_starts_at_zero():
    assert settings.DEFAULTS["budget"]["limits"]["purchases"]["per_day"] == 0.0


def test_purchasing_is_gated_even_with_no_config():
    assert safety.requires_confirmation("make_purchase")
    assert settings.DEFAULTS["autonomy"]["tools"]["make_purchase"] == settings.CONFIRM


def test_with_nothing_set_up_it_explains_the_steps(sent):
    with pytest.raises(purchasing.PurchasingUnavailable, match="deliberately several steps"):
        purchasing.make_purchase("anything", 10.0, "a thing")

    assert sent == []


def test_listing_vendors_with_none_configured_is_honest():
    result = purchasing.list_vendors()

    assert "can't spend anything" in result
    assert "no way to buy anything from a shop directly" in result


# --- the model cannot choose where money goes -------------------------------


def test_an_unknown_vendor_is_refused(monkeypatch, sent):
    """The single most important property: destinations come from config."""
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)

    with pytest.raises(ValueError, match="isn't one of the user's vendors"):
        purchasing.make_purchase("attacker-payout", 10.0, "a thing")

    assert sent == []


def test_the_tool_takes_no_url_parameter():
    """There must be no way for a model to supply a destination at all."""
    from ada.tools import specs

    spec = next(s for s in specs() if s["name"] == "make_purchase")
    properties = set(spec["input_schema"]["properties"])

    assert properties == {"vendor", "amount", "description"}
    assert not {p for p in properties if "url" in p or "endpoint" in p or "address" in p}


def test_a_vendor_url_must_be_https(monkeypatch, sent):
    _configure(
        monkeypatch,
        vendors={"dodgy": {"url": "http://synthetic.example/buy", "max_amount": 50.0}},
        purchase_budget=OPEN_BUDGET,
    )

    with pytest.raises(purchasing.PurchasingUnavailable, match="https"):
        purchasing.make_purchase("dodgy", 10.0, "a thing")

    assert sent == []


def test_a_vendor_without_a_token_cannot_be_used(monkeypatch, sent):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET, token=None)

    with pytest.raises(purchasing.PurchasingUnavailable, match="No token for"):
        purchasing.make_purchase("groceries", 10.0, "milk")

    assert sent == []


# --- the limits -------------------------------------------------------------


def test_the_budget_blocks_a_purchase_before_it_happens(monkeypatch, sent):
    """With the default zero budget, a configured vendor still can't spend."""
    _configure(monkeypatch)

    with pytest.raises(budget.BudgetExceeded):
        purchasing.make_purchase("groceries", 10.0, "milk")

    assert sent == [], "nothing may leave before the budget is checked"


def test_a_purchase_over_the_vendor_cap_is_refused(monkeypatch, sent):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)

    with pytest.raises(ValueError, match="per-purchase"):
        purchasing.make_purchase("groceries", 250.0, "a very large shop")

    assert sent == []


def test_the_refusal_tells_the_model_not_to_split_it_up(monkeypatch, sent):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)

    with pytest.raises(ValueError, match="don't split it into smaller ones"):
        purchasing.make_purchase("groceries", 250.0, "a very large shop")


@pytest.mark.parametrize("amount", [0, -5, "free", None])
def test_a_nonsense_amount_is_refused(monkeypatch, sent, amount):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)

    with pytest.raises(ValueError):
        purchasing.make_purchase("groceries", amount, "a thing")

    assert sent == []


def test_a_purchase_with_no_description_is_refused(monkeypatch, sent):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)

    with pytest.raises(ValueError, match="no description"):
        purchasing.make_purchase("groceries", 10.0, "   ")

    assert sent == []


def test_purchasing_is_rate_limited_more_tightly_than_sending():
    """Money doesn't come back."""
    assert settings.get("autonomy.limits.make_purchase.per_day") < settings.get(
        "autonomy.limits.send_message.per_day"
    )


# --- a purchase that goes through -------------------------------------------


def test_a_permitted_purchase_posts_to_the_configured_vendor(monkeypatch, sent):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)

    result = purchasing.make_purchase("groceries", 12.5, "milk and bread")

    assert len(sent) == 1
    request = sent[0]
    assert request.full_url == "https://synthetic.example/buy"
    assert request.get_header("Authorization") == "Bearer synthetic-token"
    assert b"milk and bread" in request.data
    assert "$12.50" in result


def test_a_completed_purchase_counts_against_the_budget(monkeypatch, sent):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)

    purchasing.make_purchase("groceries", 12.5, "milk")

    assert budget.spent(budget.PURCHASES, budget.WINDOWS["per_day"]) == 12.5


def test_spending_accumulates_until_the_budget_stops_it(monkeypatch, sent):
    _configure(monkeypatch, purchase_budget={"per_day": 30.0, "per_month": 500.0})

    purchasing.make_purchase("groceries", 20.0, "first shop")
    with pytest.raises(budget.BudgetExceeded):
        purchasing.make_purchase("groceries", 20.0, "second shop")

    assert len(sent) == 1, "the second never left"


def test_a_refused_purchase_does_not_count_against_the_budget(monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 402, "Payment Required", {}, None)

    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)
    monkeypatch.setattr(purchasing.urllib.request, "urlopen", refuse)

    with pytest.raises(ValueError, match="Nothing was spent"):
        purchasing.make_purchase("groceries", 12.5, "milk")

    assert budget.spent(budget.PURCHASES, budget.WINDOWS["per_day"]) == 0.0


def test_an_ambiguous_failure_counts_the_money_anyway(monkeypatch):
    """It may or may not have gone through. Under-counting lets more money
    out; over-counting only stops you slightly early."""

    def timeout(request, timeout=None):
        raise urllib.error.URLError("timed out")

    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)
    monkeypatch.setattr(purchasing.urllib.request, "urlopen", timeout)

    with pytest.raises(ValueError, match="Couldn't tell whether") as caught:
        purchasing.make_purchase("groceries", 12.5, "milk")

    assert budget.spent(budget.PURCHASES, budget.WINDOWS["per_day"]) == 12.5
    assert "check with the vendor" in str(caught.value)


# --- the confirmation -------------------------------------------------------


def test_the_confirmation_leads_with_the_money():
    description = agent._describe_action(
        "make_purchase", {"vendor": "groceries", "amount": 42.5, "description": "a weekly shop"}
    )

    assert "SPEND $42.50" in description
    assert "may not be reversible" in description
    assert "groceries" in description
    assert "a weekly shop" in description


def test_a_declined_purchase_spends_nothing(monkeypatch, sent):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET)
    monkeypatch.setattr(
        agent,
        "stream_turn",
        fake_stream_turn(
            FakeMessage(
                [
                    FakeBlock(
                        "tool_use",
                        id="t1",
                        name="make_purchase",
                        input={"vendor": "groceries", "amount": 12.5, "description": "milk"},
                    )
                ],
                "tool_use",
            ),
            FakeMessage([FakeBlock("text", text="Didn't buy it.")], "end_turn"),
        ),
    )

    agent.run_turn(
        [{"role": "user", "content": "buy milk"}],
        on_text=lambda _c: None,
        on_status=lambda _s: None,
        confirm=lambda _d: False,
    )

    assert sent == []
    assert budget.spent(budget.PURCHASES, budget.WINDOWS["per_day"]) == 0.0


def test_listing_vendors_shows_caps_and_missing_tokens(monkeypatch):
    _configure(monkeypatch, purchase_budget=OPEN_BUDGET, token=None)

    result = purchasing.list_vendors()

    assert "groceries" in result
    assert "up to $100.00" in result
    assert "no token in .env" in result
