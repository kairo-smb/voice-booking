"""Tests for /voice/balance/{shop_id} endpoint used by webapp banners."""
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
async def test_balance_status_includes_tier():
    shop_id = uuid4()
    # balance=1800 is 9% of 20000 → critical_10pct (above min_reserve of 1500)
    with patch("booking_engine.api.routes.voice_balance.get_balance",
               new=AsyncMock(return_value=1800)), \
         patch("booking_engine.api.routes.voice_balance.get_last_refill_amount",
               new=AsyncMock(return_value=20000)):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/api/v1/voice/balance/{shop_id}", headers=AUTH)
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["balance"] == 1800
            assert data["last_refill"] == 20000
            assert data["tier"] == "critical_10pct"
