"""The owner-email seam: what the webapp is asked, and how its answer is read.

The low-credit notice answers in three states rather than two, because the
sweep's bookkeeping differs: sent, or not sent for a standing reason (no owner
mailbox, webapp mail unconfigured), closes the attempt; no answer at all, or a
transient `send_failed`, leaves the episode unstamped for the next tick.
"""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from booking_engine.clients import webapp_notify
from booking_engine.config import Settings

SETTINGS = Settings(
    webapp_base_url="http://webapp.test", market_intel_secret="test-secret",
)
CREDIT_URL = "http://webapp.test/api/v1/hair-salon/whatsapp/credit-low"
TOKEN_URL = "http://webapp.test/api/v1/hair-salon/whatsapp/token-expiring"


@respx.mock
@pytest.mark.asyncio
async def test_credit_low_posts_the_balance_and_threshold():
    route = respx.post(CREDIT_URL).mock(
        return_value=httpx.Response(200, json={"data": {"sent": True}}))
    shop = uuid4()

    sent = await webapp_notify.whatsapp_credit_low(
        shop_id=shop, balance=4_200, threshold=10_000, settings=SETTINGS)

    assert sent is True
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer test-secret"
    assert json.loads(req.content) == {
        "shop_id": str(shop), "balance": 4_200, "threshold": 10_000}


@respx.mock
@pytest.mark.asyncio
async def test_credit_low_not_sent_is_an_answer():
    respx.post(CREDIT_URL).mock(return_value=httpx.Response(
        200, json={"data": {"sent": False, "reason": "no_owner_email"}}))
    assert await webapp_notify.whatsapp_credit_low(
        shop_id=uuid4(), balance=0, threshold=10_000, settings=SETTINGS) is False


@respx.mock
@pytest.mark.asyncio
async def test_credit_low_not_configured_on_the_webapp_is_an_answer():
    respx.post(CREDIT_URL).mock(return_value=httpx.Response(
        200, json={"data": {"sent": False, "reason": "not_configured"}}))
    assert await webapp_notify.whatsapp_credit_low(
        shop_id=uuid4(), balance=0, threshold=10_000, settings=SETTINGS) is False


@respx.mock
@pytest.mark.asyncio
async def test_credit_low_send_failed_is_retried_not_answered():
    """A transient Resend failure must not burn the episode's only email."""
    respx.post(CREDIT_URL).mock(return_value=httpx.Response(
        200, json={"data": {"sent": False, "reason": "send_failed"}}))
    assert await webapp_notify.whatsapp_credit_low(
        shop_id=uuid4(), balance=0, threshold=10_000, settings=SETTINGS) is None


@respx.mock
@pytest.mark.asyncio
async def test_credit_low_not_sent_without_a_known_reason_is_retried():
    respx.post(CREDIT_URL).mock(return_value=httpx.Response(
        200, json={"data": {"sent": False}}))
    assert await webapp_notify.whatsapp_credit_low(
        shop_id=uuid4(), balance=0, threshold=10_000, settings=SETTINGS) is None


@respx.mock
@pytest.mark.asyncio
async def test_credit_low_without_an_answer_is_none():
    respx.post(CREDIT_URL).mock(return_value=httpx.Response(500))
    assert await webapp_notify.whatsapp_credit_low(
        shop_id=uuid4(), balance=0, threshold=10_000, settings=SETTINGS) is None


@pytest.mark.asyncio
async def test_credit_low_unconfigured_is_none():
    assert await webapp_notify.whatsapp_credit_low(
        shop_id=uuid4(), balance=0, threshold=10_000, settings=Settings(
            webapp_base_url="", market_intel_secret="")) is None


@respx.mock
@pytest.mark.asyncio
async def test_token_expiring_keeps_its_wire_contract():
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"data": {"sent": True}}))
    shop = uuid4()

    assert await webapp_notify.whatsapp_token_expiring(
        shop_id=shop, days_left=3, phone_number=None, settings=SETTINGS) is True
    assert json.loads(route.calls[0].request.content) == {
        "shop_id": str(shop), "days_left": 3, "phone_number": ""}

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))
    assert await webapp_notify.whatsapp_token_expiring(
        shop_id=shop, days_left=3, phone_number=None, settings=SETTINGS) is False
