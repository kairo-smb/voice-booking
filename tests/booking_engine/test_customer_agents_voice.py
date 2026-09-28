"""The wire contract with marketing-engine's voice surface of the customer agents.

The realtime session's tools are served by marketing-engine
(`/customer-agents/voice/mcp`) and its agent rules come from
`/customer-agents/voice/instructions`, both authorized with the per-call token
this repo mints. A failure to fetch the rules must never cost the call.
"""
from __future__ import annotations

import logging
from uuid import uuid4

import httpx
import pytest
import respx

from booking_engine.clients import customer_agents_voice as cav
from booking_engine.config import Settings

BASE = "http://market-intel.test"


def _settings(**kw):
    return Settings(market_intel_api_url=BASE + "/", **kw)


def test_mcp_url_points_at_the_customer_agents_voice_surface():
    assert cav.mcp_url(_settings()) == f"{BASE}/customer-agents/voice/mcp"


def test_mcp_url_is_none_when_unconfigured():
    assert cav.mcp_url(Settings(market_intel_api_url="")) is None


@respx.mock
@pytest.mark.asyncio
async def test_fetch_instructions_sends_the_call_token_and_the_ids():
    shop, call = uuid4(), uuid4()
    route = respx.get(f"{BASE}/customer-agents/voice/instructions").mock(
        return_value=httpx.Response(200, json={"instructions": "REGOLE"}))

    out = await cav.fetch_instructions(
        shop_id=shop, call_id=call, token="tok.sig", settings=_settings())

    assert out == "REGOLE"
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer tok.sig"
    assert req.url.params["shop_id"] == str(shop)
    assert req.url.params["call_id"] == str(call)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"error": "Unauthorized"}),
    httpx.Response(500, text="boom"),
    httpx.Response(200, json={"nope": 1}),
    httpx.Response(200, text="not json"),
])
async def test_fetch_instructions_fails_to_none_and_logs_error(response, caplog):
    respx.get(f"{BASE}/customer-agents/voice/instructions").mock(return_value=response)
    with caplog.at_level(logging.ERROR):
        out = await cav.fetch_instructions(
            shop_id=uuid4(), call_id=uuid4(), token="t", settings=_settings())
    assert out is None
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@respx.mock
@pytest.mark.asyncio
async def test_fetch_instructions_survives_an_unreachable_engine(caplog):
    respx.get(f"{BASE}/customer-agents/voice/instructions").mock(
        side_effect=httpx.ConnectError("down"))
    with caplog.at_level(logging.ERROR):
        out = await cav.fetch_instructions(
            shop_id=uuid4(), call_id=uuid4(), token="t", settings=_settings())
    assert out is None
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_fetch_instructions_unconfigured_is_none_and_logged(caplog):
    with caplog.at_level(logging.ERROR):
        out = await cav.fetch_instructions(
            shop_id=uuid4(), call_id=uuid4(), token="t",
            settings=Settings(market_intel_api_url=""))
    assert out is None
    assert any(r.levelno == logging.ERROR for r in caplog.records)
