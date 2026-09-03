"""Tests for the webapp AI-credit charge client — the voice/SMS charge seam.

This is the contract with the webapp's charge-actual endpoint: the meter's
pre-converted credits amount is POSTed as `credits` (never USD — the margin
rule lives webapp-side), the run kind/value travels as `run_type`, and a 402
(empty basket) is a refusal, returned as ok:False so callers log and move on.
"""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from booking_engine.clients import webapp_credits
from booking_engine.config import Settings

SETTINGS = Settings(
    webapp_base_url="http://webapp.test", market_intel_secret="test-secret",
)
URL = "http://webapp.test/api/v1/hair-salon/marketing/charge-actual"


@respx.mock
@pytest.mark.asyncio
async def test_charge_actual_posts_pre_converted_credits():
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json={
            "data": {"ok": True, "actual_credits": 3440, "remaining": 100},
        }),
    )
    shop_id = uuid4()
    call_id = uuid4()

    ok = await webapp_credits.charge_actual(
        shop_id=shop_id, run_type=webapp_credits.VOICE_CALL,
        run_ref=str(call_id), credits=3440, settings=SETTINGS,
    )

    assert ok is True
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer test-secret"
    payload = json.loads(req.content)
    assert payload["shop_id"] == str(shop_id)
    assert payload["run_type"] == "voice_call"   # → ai_run_ledger.run_kind webapp-side
    assert payload["credits"] == 3440
    assert payload["run_ref"] == str(call_id)
    # USD and credits are mutually exclusive inputs — never both.
    assert "llm_usd" not in payload
    assert "apify_usd" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_charge_actual_omits_run_ref_when_none():
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={
            "data": {"ok": True, "actual_credits": 186, "remaining": 0},
        }),
    )

    ok = await webapp_credits.charge_actual(
        shop_id=uuid4(), run_type=webapp_credits.SMS_SEND,
        run_ref=None, credits=186, settings=SETTINGS,
    )

    assert ok is True


@respx.mock
@pytest.mark.asyncio
async def test_charge_actual_402_is_a_refusal_not_a_charge():
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            402, json={"error": "insufficient_credits", "required": 5000},
        ),
    )

    ok = await webapp_credits.charge_actual(
        shop_id=uuid4(), run_type=webapp_credits.VOICE_CALL,
        run_ref=str(uuid4()), credits=3440, settings=SETTINGS,
    )

    assert ok is False
    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_charge_actual_fails_closed_when_unconfigured():
    # No WEBAPP_BASE_URL / MARKET_INTEL_SECRET → refuse without a network call.
    ok = await webapp_credits.charge_actual(
        shop_id=uuid4(), run_type=webapp_credits.VOICE_CALL,
        run_ref=str(uuid4()), credits=3440, settings=Settings(),
    )
    assert ok is False


@pytest.mark.asyncio
async def test_charge_actual_unreachable_returns_false(monkeypatch):
    class _ExplodingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(webapp_credits.httpx, "AsyncClient", _ExplodingClient)
    ok = await webapp_credits.charge_actual(
        shop_id=uuid4(), run_type=webapp_credits.VOICE_CALL,
        run_ref=str(uuid4()), credits=3440, settings=SETTINGS,
    )
    assert ok is False
