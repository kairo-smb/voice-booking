"""Tests for /voice/numbers/* control-plane endpoints."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")


@pytest.mark.asyncio
async def test_search_numbers_requires_auth():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/voice/numbers/search?area_code=02")
        assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_search_numbers_returns_list():
    from booking_engine.clients.twilio_numbers import AvailableNumber
    with patch("booking_engine.api.routes.voice_telephony.search_available_numbers",
               return_value=[
                   AvailableNumber(phone_number="+390212345678",
                                   friendly_name="x", locality="Milano", region="L"),
               ]):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/api/v1/voice/numbers/search?area_code=02", headers=AUTH)
            assert r.status_code == 200
            body = r.json()
            assert body["data"][0]["phone_number"] == "+390212345678"


@pytest.mark.asyncio
async def test_provision_writes_telephony_row():
    from booking_engine.clients.twilio_numbers import PurchasedNumber
    with patch("booking_engine.api.routes.voice_telephony.purchase_number",
               return_value=PurchasedNumber(sid="PN1", phone_number="+390212345678")), \
         patch("booking_engine.db.voice_telephony_queries.upsert_telephony",
               return_value={
                   "shop_id": "00000000-0000-0000-0000-000000000001",
                   "kairo_number": "+390212345678",
                   "kairo_number_sid": "PN1",
                   "setup_path": "new",
                   "salon_existing_number": None,
               }):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/numbers/provision",
                headers=AUTH,
                json={
                    "shop_id": "00000000-0000-0000-0000-000000000001",
                    "phone_number": "+390212345678",
                    "setup_path": "new",
                },
            )
            assert r.status_code == 200
            body = r.json()
            assert body["data"]["kairo_number"] == "+390212345678"
