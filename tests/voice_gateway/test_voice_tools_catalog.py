"""Tests for catalog tool endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app

_app = create_app()

AUTH = {"Authorization": "Bearer tool-secret", "X-Shop-Id": str(uuid4()),
        "X-Call-Id": str(uuid4())}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_get_services_omits_price_by_default():
    sid = uuid4()
    fake = [{"id": sid, "name": "Taglio donna", "duration_min": 30, "price_cents": 2500}]
    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/tools/get_services",
                             headers=AUTH, json={"filter": "taglio"})
    body = r.json()
    assert body["ok"] is True
    assert body["data"][0]["service_id"] == str(sid)
    assert body["data"][0]["price_cents"] is None


@pytest.mark.asyncio
async def test_get_services_include_price_true_returns_price():
    sid = uuid4()
    fake = [{"id": sid, "name": "Taglio donna", "duration_min": 30, "price_cents": 2500}]
    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/voice/tools/get_services", headers=AUTH,
                             json={"filter": "taglio", "include_price": True})
    body = r.json()
    assert body["ok"] is True
    assert body["data"][0]["price_cents"] == 2500


@pytest.mark.asyncio
async def test_get_staff_for_service_returns_staff():
    staff_id = uuid4()
    fake = [{"id": staff_id, "name": "Giulia"}]
    with patch("booking_engine.api.routes.voice_tools_catalog.list_staff_for_service",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/get_staff_for_service",
                headers=AUTH, json={"service_id": str(uuid4())},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"][0]["name"] == "Giulia"