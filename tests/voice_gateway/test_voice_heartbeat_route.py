"""Tests for /api/v1/voice/heartbeat/forwarding scheduled endpoint."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_heartbeat_requires_auth():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/v1/voice/heartbeat/forwarding")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_heartbeat_returns_emitted_count():
    with patch(
        "booking_engine.api.routes.voice_heartbeat.emit_heartbeat_alerts",
        new=AsyncMock(return_value=3),
    ):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/voice/heartbeat/forwarding", headers=AUTH)
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["alerts_emitted"] == 3
    assert body["threshold_days"] == 5
