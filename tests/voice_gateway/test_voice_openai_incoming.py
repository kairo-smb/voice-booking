"""OpenAI realtime.call.incoming webhook -> accept the call with our tools."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app
from booking_engine.services.call_token import verify_call_token
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


def _patches(accept, fetch, call_id):
    return (
        patch("booking_engine.api.routes.voice_openai.get_config",
              new=AsyncMock(return_value=_config())),
        patch("booking_engine.api.routes.voice_openai.get_policy",
              new=AsyncMock(return_value={"disclosure_text": "Salve, AI."})),
        patch("booking_engine.api.routes.voice_openai.resolve_caller",
              new=AsyncMock(return_value=ResolutionResult(is_anonymous=False, matches=[]))),
        patch("booking_engine.api.routes.voice_openai.insert_call",
              new=AsyncMock(return_value=call_id)),
        patch("booking_engine.api.routes.voice_openai.fetch_instructions", new=fetch),
        patch("booking_engine.api.routes.voice_openai.accept_sip_call", new=accept),
    )


async def _post_incoming(accept, fetch, call_id, shop):
    p = _patches(accept, fetch, call_id)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/voice/openai/incoming", json=_event(shop))


@pytest.mark.asyncio
async def test_incoming_points_the_session_at_the_customer_agents_layer(monkeypatch):
    monkeypatch.setenv("MARKET_INTEL_API_URL", "https://mi.test")
    monkeypatch.setenv("VOICE_AGENT_TOOL_SECRET", "s3cret")
    shop, db_call = uuid4(), uuid4()
    accept = AsyncMock(return_value=True)
    fetch = AsyncMock(return_value="REGOLE_DAL_MOTORE")

    r = await _post_incoming(accept, fetch, db_call, shop)

    assert r.status_code == 200
    assert r.json() == {"status": "accepted"}
    accept.assert_awaited_once()
    kwargs = accept.await_args.kwargs
    assert kwargs["call_id"] == "rtc_123"
    payload = kwargs["payload"]
    assert payload["model"] == "gpt-realtime"
    [mcp] = payload["tools"]
    assert mcp["type"] == "mcp"
    assert mcp["server_url"] == "https://mi.test/customer-agents/voice/mcp"
    # The per-call token: our session row's ids, signed with the tool secret —
    # the same token authorizes the instructions fetch.
    claims = verify_call_token(token=mcp["authorization"], secret="s3cret")
    assert claims == {"shop_id": str(shop), "call_id": str(db_call)}
    assert "escalate_to_owner" in mcp["allowed_tools"]
    assert "create_booking" not in mcp["allowed_tools"]
    fetch.assert_awaited_once()
    fk = fetch.await_args.kwargs
    assert fk["shop_id"] == shop and fk["call_id"] == db_call
    assert fk["token"] == mcp["authorization"]
    assert payload["instructions"].rstrip().endswith("REGOLE_DAL_MOTORE")


@pytest.mark.asyncio
async def test_incoming_still_accepts_with_persona_only_when_rules_fetch_fails(monkeypatch):
    monkeypatch.setenv("MARKET_INTEL_API_URL", "https://mi.test")
    monkeypatch.setenv("VOICE_AGENT_TOOL_SECRET", "s3cret")
    accept = AsyncMock(return_value=True)
    fetch = AsyncMock(return_value=None)  # the client already logged the error

    r = await _post_incoming(accept, fetch, uuid4(), uuid4())

    assert r.json() == {"status": "accepted"}
    payload = accept.await_args.kwargs["payload"]
    assert "Salone Lucia" in payload["instructions"]
    assert payload["tools"][0]["type"] == "mcp"


@pytest.mark.asyncio
async def test_incoming_unconfigured_engine_accepts_without_tools_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("MARKET_INTEL_API_URL", "")
    monkeypatch.setenv("VOICE_AGENT_TOOL_SECRET", "s3cret")
    accept = AsyncMock(return_value=True)
    fetch = AsyncMock(return_value=None)

    with caplog.at_level(logging.ERROR):
        r = await _post_incoming(accept, fetch, uuid4(), uuid4())

    assert r.json() == {"status": "accepted"}
    assert accept.await_args.kwargs["payload"]["tools"] == []
    fetch.assert_not_awaited()
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


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
