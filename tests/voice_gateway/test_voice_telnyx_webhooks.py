"""Tests for POST /voice/telnyx/number-status webhook."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app


def _active_payload(phone_number: str = "+390212345678") -> dict:
    return {
        "data": {
            "event_type": "number.status.active",
            "payload": {"phone_number": phone_number},
        }
    }


def _failed_payload(
    phone_number: str = "+390212345678",
    reason: str = "ID document expired",
) -> dict:
    return {
        "data": {
            "event_type": "number.status.failed",
            "payload": {
                "phone_number": phone_number,
                "regulatory_rejection_reason": reason,
            },
        }
    }


@pytest.mark.asyncio
async def test_active_event_fires_push():
    shop_id = uuid4()
    with patch(
        "booking_engine.api.routes.voice_telnyx_webhooks.update_telephony_activation",
        new=AsyncMock(return_value={"shop_id": shop_id, "kairo_number": "+390212345678"}),
    ), patch(
        "booking_engine.api.routes.voice_telnyx_webhooks.send_push",
        new=AsyncMock(return_value=None),
    ) as push:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/voice/telnyx/number-status", json=_active_payload())
    assert r.status_code == 200
    assert r.json()["status"] == "activated"
    push.assert_awaited_once()
    assert push.await_args.kwargs["event"] == "voice_number_activated"


@pytest.mark.asyncio
async def test_failed_event_persists_reason_and_fires_push():
    shop_id = uuid4()
    with patch(
        "booking_engine.api.routes.voice_telnyx_webhooks.update_telephony_activation",
        new=AsyncMock(return_value={"shop_id": shop_id, "kairo_number": "+390212345678"}),
    ) as upd, patch(
        "booking_engine.api.routes.voice_telnyx_webhooks.send_push",
        new=AsyncMock(return_value=None),
    ) as push:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/telnyx/number-status",
                json=_failed_payload(reason="ID document expired"),
            )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    upd.assert_awaited_once()
    assert upd.await_args.kwargs["regulatory_rejection_reason"] == "ID document expired"
    push.assert_awaited_once()
    assert push.await_args.kwargs["event"] == "voice_number_rejected"


@pytest.mark.asyncio
async def test_unknown_number_returns_gracefully():
    with patch(
        "booking_engine.api.routes.voice_telnyx_webhooks.update_telephony_activation",
        new=AsyncMock(return_value=None),
    ):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/voice/telnyx/number-status", json=_active_payload())
    assert r.status_code == 200
    assert r.json()["status"] == "unknown_number"


@pytest.mark.asyncio
async def test_unknown_event_type_is_ignored():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/voice/telnyx/number-status",
            json={"data": {"event_type": "number.order.created", "payload": {"phone_number": "+390212345678"}}},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_malformed_payload_returns_400():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/voice/telnyx/number-status", json={"bad": "payload"})
    assert r.status_code == 400
