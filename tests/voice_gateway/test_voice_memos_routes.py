"""Tests for the webapp-facing /voice/memos endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app

_app = create_app()

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "test-secret")
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_list_pending_memos():
    shop_id = uuid4()
    fake = [{"id": uuid4(), "call_id": uuid4(), "shop_id": shop_id,
             "customer_id": None, "caller_phone": "+393201234567",
             "reason": "Vuole cambiare data", "callback_window": "oggi pomeriggio",
             "status": "pending", "actioned_by": None, "actioned_at": None,
             "created_at": "2026-06-03T14:32:00Z"}]
    with patch("booking_engine.api.routes.voice_memos.list_memos",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/voice/memos/{shop_id}?status=pending", headers=AUTH)
    body = r.json()
    assert body["data"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_action_memo_updates_status():
    memo_id = uuid4()
    staff_id = uuid4()
    with patch("booking_engine.api.routes.voice_memos.update_memo_status",
               new=AsyncMock(return_value=True)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.patch(
                f"/voice/memos/{memo_id}",
                headers=AUTH,
                json={"status": "actioned", "actioned_by": str(staff_id)},
            )
    assert r.json()["data"]["updated"] is True