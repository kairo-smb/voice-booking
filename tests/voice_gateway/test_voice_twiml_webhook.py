"""Tests for the dynamic TwiML webhook (per-call routing decision)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport
from twilio.request_validator import RequestValidator

from booking_engine.api.app import create_app


def _form_data(called: str = "+37251234567", from_: str = "+393201234567"):
    return {"Called": called, "From": from_, "CallSid": "CA123"}


def _config(enabled: bool = True, fallback: str | None = None):
    return {"shop_id": uuid4(), "enabled": enabled,
            "manual_fallback_number": fallback,
            "manual_fallback_normalized": (fallback or "").replace("+", "")}


def _telephony(salon_existing: str | None = None):
    return {"shop_id": uuid4(),
            "kairo_number": "+37251234567",
            "salon_existing_number": salon_existing,
            "salon_existing_normalized": (salon_existing or "").replace("+", "")}


@pytest.mark.asyncio
async def test_twiml_attaches_when_basket_ok():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": True, "balance": 5000, "detach_reason": None
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert r.status_code == 200
            assert "<Sip>" in r.text
            assert "openai.com" in r.text


@pytest.mark.asyncio
async def test_twiml_routes_to_fallback_when_detached_with_fallback():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True, fallback="+393900000000"))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": False, "balance": 200, "detach_reason": "basket_low"
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert "<Dial" in r.text
            assert "+393900000000" in r.text
            assert "<Sip>" not in r.text


@pytest.mark.asyncio
async def test_twiml_plays_say_when_detached_without_fallback():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True, fallback=None))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": False, "balance": 0, "detach_reason": "basket_low"
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert "<Say" in r.text
            assert "<Dial" not in r.text


@pytest.mark.asyncio
async def test_twiml_loop_safety_falls_back_to_say():
    # fallback equals the salon's forwarded number -> loop risk -> play Say instead
    salon_existing = "+393900000000"
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony(salon_existing=salon_existing))), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True, fallback=salon_existing))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": False, "balance": 0, "detach_reason": "basket_low"
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert "<Say" in r.text
            assert "<Dial" not in r.text


@pytest.mark.asyncio
async def test_twiml_returns_say_when_unknown_number():
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=None)):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/twiml/incoming", data=_form_data())
            assert r.status_code == 200
            assert "<Say" in r.text


@pytest.mark.asyncio
async def test_twiml_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/twiml/incoming",
                data=_form_data(),
                headers={"X-Twilio-Signature": "bogus"},
            )
            assert r.status_code == 403


@pytest.mark.asyncio
async def test_twiml_accepts_valid_signature(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    form = _form_data()
    url = "https://api.example.com/api/v1/voice/twiml/incoming"
    signature = RequestValidator("test-token").compute_signature(url, form)
    with patch("booking_engine.api.routes.voice_twiml.get_telephony_by_kairo_number",
               new=AsyncMock(return_value=_telephony())), \
         patch("booking_engine.api.routes.voice_twiml.get_config",
               new=AsyncMock(return_value=_config(enabled=True))), \
         patch("booking_engine.api.routes.voice_twiml.decide_session",
               new=AsyncMock(return_value=type("D", (), {
                   "attach": True, "balance": 5000, "detach_reason": None
               })())):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/twiml/incoming",
                data=form,
                headers={"X-Twilio-Signature": signature},
            )
            assert r.status_code == 200
            assert "<Sip>" in r.text
