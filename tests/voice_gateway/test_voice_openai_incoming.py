"""OpenAI realtime.call.incoming webhook -> accept the call with our tools."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app
from booking_engine.services.identity_resolver import ResolutionResult

_app = create_app()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_REALTIME_MODEL", "gpt-realtime")


def _config():
    return {"display_name": "Salone Lucia", "greeting_after_disclosure": "Ciao.",
            "greeting_overflow": "", "tone_id": None, "voice_preset": "verse",
            "answer_mode": "always_on", "enabled": True}


def _event(shop_id):
    return {"type": "realtime.call.incoming", "data": {
        "call_id": "rtc_123",
        "sip_headers": [
            {"name": "From", "value": "sip:+393331112222@sip.example.com"},
            {"name": "X-Shop-Id", "value": str(shop_id)},
        ],
    }}


@pytest.mark.asyncio
async def test_incoming_accepts_call_with_tools_registered():
    shop = uuid4()
    accept = AsyncMock(return_value=True)
    with patch("booking_engine.api.routes.voice_openai.get_config",
               new=AsyncMock(return_value=_config())), \
         patch("booking_engine.api.routes.voice_openai.get_policy",
               new=AsyncMock(return_value={"disclosure_text": "Salve, AI."})), \
         patch("booking_engine.api.routes.voice_openai.resolve_caller",
               new=AsyncMock(return_value=ResolutionResult(is_anonymous=False, matches=[]))), \
         patch("booking_engine.api.routes.voice_openai.insert_call",
               new=AsyncMock(return_value=uuid4())), \
         patch("booking_engine.api.routes.voice_openai.accept_sip_call", new=accept):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/openai/incoming", json=_event(shop))
    assert r.status_code == 200
    accept.assert_awaited_once()
    kwargs = accept.await_args.kwargs
    assert kwargs["call_id"] == "rtc_123"
    payload = kwargs["payload"]
    assert payload["model"] == "gpt-realtime"
    names = {t["name"] for t in payload["tools"]}
    assert "create_booking" in names and "escalate_to_merchant" in names


@pytest.mark.asyncio
async def test_incoming_ignores_unrelated_event_and_does_not_accept():
    accept = AsyncMock(return_value=True)
    with patch("booking_engine.api.routes.voice_openai.accept_sip_call", new=accept):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/openai/incoming",
                             json={"type": "realtime.call.ended", "data": {}})
    assert r.status_code == 200
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_incoming_unroutable_without_fallback_when_shop_header_missing():
    accept = AsyncMock(return_value=True)
    event = {"type": "realtime.call.incoming", "data": {
        "call_id": "rtc_123",
        "sip_headers": [{"name": "From", "value": "sip:+393331112222@sip.example.com"}],
    }}
    with patch("booking_engine.api.routes.voice_openai.accept_sip_call", new=accept):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/openai/incoming", json=event)
    assert r.json() == {"status": "unroutable"}
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_incoming_uses_test_fallback_shop_when_header_missing(monkeypatch):
    shop = uuid4()
    monkeypatch.setenv("SIP_TEST_FALLBACK_SHOP_ID", str(shop))
    accept = AsyncMock(return_value=True)
    event = {"type": "realtime.call.incoming", "data": {
        "call_id": "rtc_123",
        "sip_headers": [{"name": "From", "value": "sip:+393331112222@sip.example.com"}],
    }}
    with patch("booking_engine.api.routes.voice_openai.get_config",
               new=AsyncMock(return_value=_config())), \
         patch("booking_engine.api.routes.voice_openai.get_policy",
               new=AsyncMock(return_value={"disclosure_text": "Salve, AI."})), \
         patch("booking_engine.api.routes.voice_openai.resolve_caller",
               new=AsyncMock(return_value=ResolutionResult(is_anonymous=False, matches=[]))), \
         patch("booking_engine.api.routes.voice_openai.insert_call",
               new=AsyncMock(return_value=uuid4())), \
         patch("booking_engine.api.routes.voice_openai.accept_sip_call", new=accept):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/openai/incoming", json=event)
    assert r.status_code == 200
    accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_incoming_unknown_shop_does_not_accept():
    accept = AsyncMock(return_value=True)
    with patch("booking_engine.api.routes.voice_openai.get_config",
               new=AsyncMock(return_value=None)), \
         patch("booking_engine.api.routes.voice_openai.accept_sip_call", new=accept):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/openai/incoming", json=_event(uuid4()))
    assert r.status_code == 200
    accept.assert_not_awaited()
