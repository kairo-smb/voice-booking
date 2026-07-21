"""Tests for /voice/config/{shop_id} GET and PATCH."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_get_config_returns_existing():
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_config.get_config",
               new=AsyncMock(return_value={
                   "shop_id": shop_id, "enabled": True,
                   "display_name": "Salone Lucia",
                   "greeting_after_disclosure": "Ciao!",
                   "voice_preset": "verse", "tone_id": None,
                   "business_hours": {}, "answer_mode": "overflow",
                   "overflow_ring_count": 4, "services_to_mention": [],
                   "retention_days": 90, "manual_fallback_number": None,
                   "auto_topup_enabled": False,
                   "auto_topup_threshold_tokens": None,
                   "auto_topup_package_id": None,
               })):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/api/v1/voice/config/{shop_id}", headers=AUTH)
            assert r.status_code == 200
            assert r.json()["data"]["display_name"] == "Salone Lucia"


@pytest.mark.asyncio
async def test_patch_rejects_fallback_equals_forwarded():
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_config.get_telephony",
               new=AsyncMock(return_value={
                   "salon_existing_normalized": "393900000000",
               })):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.patch(
                f"/api/v1/voice/config/{shop_id}",
                headers=AUTH,
                json={"manual_fallback_number": "+39 390 000 0000"},
            )
            assert r.status_code == 400
            assert "loop" in r.json()["error"].lower()


@pytest.mark.asyncio
async def test_patch_accepts_distinct_fallback():
    shop_id = uuid4()
    with patch("booking_engine.api.routes.voice_config.get_telephony",
               new=AsyncMock(return_value={
                   "salon_existing_normalized": "393900000000",
               })), \
         patch("booking_engine.api.routes.voice_config.upsert_config",
               new=AsyncMock(return_value={
                   "shop_id": shop_id, "enabled": True,
                   "manual_fallback_number": "+393201234567",
                   "display_name": "", "greeting_after_disclosure": "",
                   "voice_preset": "verse", "tone_id": None,
                   "business_hours": {}, "answer_mode": "overflow",
                   "overflow_ring_count": 4, "services_to_mention": [],
                   "retention_days": 90,
                   "auto_topup_enabled": False,
                   "auto_topup_threshold_tokens": None,
                   "auto_topup_package_id": None,
               })):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.patch(
                f"/api/v1/voice/config/{shop_id}",
                headers=AUTH,
                json={"manual_fallback_number": "+393201234567"},
            )
            assert r.status_code == 200
            assert r.json()["data"]["manual_fallback_number"] == "+393201234567"


@pytest.mark.asyncio
async def test_patch_accepts_greeting_overflow():
    shop_id = uuid4()
    captured = {}

    async def fake_upsert(sid, **kw):
        captured.update(kw)
        return {"shop_id": sid, **kw}

    with patch("booking_engine.api.routes.voice_config.upsert_config",
               new=fake_upsert):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.patch(
                f"/api/v1/voice/config/{shop_id}",
                headers=AUTH,
                json={"greeting_overflow": "Salve, sono l'assistente."},
            )
            assert r.status_code == 200
            assert captured["greeting_overflow"] == "Salve, sono l'assistente."
