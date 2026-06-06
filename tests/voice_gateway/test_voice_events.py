"""Tests for OpenAI session-lifecycle webhooks."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app
from booking_engine.services.identity_resolver import ResolutionResult

_app = create_app()

AUTH = {"Authorization": "Bearer tool-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_session_started_returns_assembled_prompt():
    shop_id = uuid4()
    call_id = uuid4()
    config = {
        "display_name": "Salone Lucia",
        "greeting_after_disclosure": "Sono Aria",
        "voice_preset": "warm_female", "tone_preset": "warm",
        "answer_mode": "overflow", "services_to_mention": [],
    }
    policy = {"disclosure_text": "Salve, assistente AI...",
              "recording_consent_prompt": "Posso aiutarla?"}
    with patch("booking_engine.api.routes.voice_events.get_config",
               new=AsyncMock(return_value=config)), \
         patch("booking_engine.api.routes.voice_events.get_policy",
               new=AsyncMock(return_value=policy)), \
         patch("booking_engine.api.routes.voice_events.resolve_caller",
               new=AsyncMock(return_value=ResolutionResult(is_anonymous=False, matches=[]))), \
         patch("booking_engine.api.routes.voice_events.insert_call",
               new=AsyncMock(return_value=call_id)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/events/session.started",
                headers={**AUTH, "X-Shop-Id": str(shop_id)},
                json={"caller_phone": "+393201234567",
                      "openai_session_id": "sess_123"},
            )
    body = r.json()
    assert "prompt" in body["data"]
    assert "tools" in body["data"]
    assert body["data"]["call_id"] == str(call_id)


@pytest.mark.asyncio
async def test_session_turn_appends_to_transcript():
    call_id = uuid4()
    with patch("booking_engine.api.routes.voice_events.insert_call_turn",
               new=AsyncMock(return_value=None)) as fn:
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                f"/voice/events/session.turn",
                headers={**AUTH, "X-Call-Id": str(call_id)},
                json={"role": "caller", "text": "Ciao!", "seq": 1},
            )
    assert r.json()["ok"] is True
    fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_ended_finalizes_and_debits():
    call_id = uuid4()
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_events.finalize_call",
               new=AsyncMock(return_value=None)), \
         patch("booking_engine.api.routes.voice_events.get_call",
               new=AsyncMock(return_value={
                   "id": call_id, "shop_id": shop_id, "outcome": "booked",
               })), \
         patch("booking_engine.api.routes.voice_events.record_voice_debit",
               new=AsyncMock(return_value=None)) as debit:
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/events/session.ended",
                headers={**AUTH, "X-Call-Id": str(call_id)},
                json={"duration_seconds": 180, "tool_token_cost": 200,
                      "ended_at": datetime.now(timezone.utc).isoformat()},
            )
    assert r.json()["ok"] is True
    debit.assert_awaited_once()