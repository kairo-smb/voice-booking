"""Tests for the marketing-engine triage client.

This is the contract with marketing-engine's `/whatsapp/triage` gateway: it
names an inbound WhatsApp message's intent (booking, cancel, complaint, ...)
before anything can route it. Every failure — unconfigured, missing secret,
402 (the salon has no AI credit), malformed body, timeout, 5xx — must return
`None`. `None` means "unrouted", which means a human looks at the thread.
There is no path here that invents or guesses a verdict, because a wrong
verdict routes a real customer to the wrong handler and nobody finds out.

No exception may ever escape `classify()`. It is awaited from a fire-and-
forget background task; an uncaught exception there is a silently dropped
customer message.
"""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from booking_engine.clients import marketing_triage as triage
from booking_engine.config import Settings

SHOP = uuid4()
URL = "http://market-intel.test/whatsapp/triage"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        market_intel_api_url="http://market-intel.test",
        market_intel_secret="test-secret",
    )


@pytest.fixture
def settings_without_url() -> Settings:
    return Settings(market_intel_api_url="", market_intel_secret="test-secret")


@respx.mock
@pytest.mark.asyncio
async def test_posts_the_text_with_the_shared_bearer(settings):
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"intent": "booking", "confidence": 0.9, "summary": "s"},
                "llm_cost_usd": 0.0016,
            },
        ),
    )

    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)

    assert out == {"intent": "booking", "confidence": 0.9, "summary": "s"}
    req = route.calls[0].request
    assert req.headers["authorization"] == f"Bearer {settings.market_intel_secret}"
    assert str(req.url).endswith("/whatsapp/triage")
    payload = json.loads(req.content)
    assert payload["shop_id"] == str(SHOP)
    assert isinstance(payload["shop_id"], str)  # survives JSON serialisation
    assert payload["text"] == "ciao"


@pytest.mark.asyncio
async def test_unconfigured_returns_none_and_does_not_guess(settings_without_url):
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings_without_url)
    assert out is None


@pytest.mark.asyncio
async def test_missing_secret_also_fails_closed():
    # URL set, secret empty — both halves are required, same as webapp_credits.
    settings = Settings(
        market_intel_api_url="http://market-intel.test", market_intel_secret="",
    )
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None


@respx.mock
@pytest.mark.asyncio
async def test_a_402_is_a_refusal_not_a_crash(settings):
    respx.post(URL).mock(
        return_value=httpx.Response(402, json={"error": "insufficient_credits"}),
    )
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None


@respx.mock
@pytest.mark.asyncio
async def test_a_500_returns_none(settings):
    respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None


@respx.mock
@pytest.mark.asyncio
async def test_200_with_no_data_key_returns_none(settings):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"llm_cost_usd": 0.001}))
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None


@respx.mock
@pytest.mark.asyncio
async def test_200_with_data_as_a_string_returns_none(settings):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"data": "booking"}))
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None


@respx.mock
@pytest.mark.asyncio
async def test_200_with_data_as_a_list_returns_none(settings):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"data": ["booking"]}))
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None


@respx.mock
@pytest.mark.asyncio
async def test_malformed_json_returns_none(settings):
    respx.post(URL).mock(return_value=httpx.Response(200, text="not json at all"))
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None


@pytest.mark.asyncio
async def test_timeout_returns_none(monkeypatch, settings):
    class _TimingOutClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(triage.httpx, "AsyncClient", _TimingOutClient)
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None


@pytest.mark.asyncio
async def test_no_exception_escapes_classify_ever(monkeypatch, settings):
    # Something wildly unanticipated downstream — a bare exception that is
    # not even an httpx.HTTPError subclass. classify() must still return
    # None, never raise, since it runs from a fire-and-forget background task.
    class _ExplodingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise RuntimeError("something wildly unexpected")

    monkeypatch.setattr(triage.httpx, "AsyncClient", _ExplodingClient)
    out = await triage.classify(shop_id=SHOP, text="ciao", settings=settings)
    assert out is None
