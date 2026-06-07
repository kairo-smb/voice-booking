"""Tests for booking tool endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
async def test_check_availability_returns_slots():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    slot_start = now + timedelta(days=1, hours=10)
    slot_end = slot_start + timedelta(minutes=30)
    fake = [{"slot_start": slot_start, "slot_end": slot_end,
             "staff_id": uuid4(), "staff_name": "Giulia"}]
    with patch("booking_engine.api.routes.voice_tools_booking.find_availability",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/check_availability",
                headers=AUTH,
                json={"service_id": str(uuid4()), "max_results": 5},
            )
    body = r.json()
    assert body["ok"] is True
    assert len(body["data"]) == 1


@pytest.mark.asyncio
async def test_create_booking_inserts_and_attaches_to_call():
    appt_id = uuid4()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=AsyncMock(return_value={
                   "id": appt_id, "slot_start": now,
                   "slot_end": now + timedelta(minutes=30),
                   "staff_id": uuid4(),
                   "confirmation_status": "confirmed",
               })), \
         patch("booking_engine.api.routes.voice_tools_booking.attach_booking_to_call",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()), "service_id": str(uuid4()),
                      "slot_start": now.isoformat(), "staff_id": str(uuid4())},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["appointment_id"] == str(appt_id)


@pytest.mark.asyncio
async def test_create_booking_slot_taken_returns_error():
    with patch("booking_engine.api.routes.voice_tools_booking.insert_booking_locked",
               new=AsyncMock(side_effect=RuntimeError("slot_taken"))):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_booking",
                headers=AUTH,
                json={"customer_id": str(uuid4()), "service_id": str(uuid4()),
                      "slot_start": datetime.now(timezone.utc).isoformat(),
                      "staff_id": str(uuid4())},
            )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "slot_taken"


@pytest.mark.asyncio
async def test_modify_booking_requires_verification():
    with patch("booking_engine.api.routes.voice_tools_booking.log_auth_event",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/modify_booking",
                headers=AUTH,
                json={"appointment_id": str(uuid4()),
                      "verification_passed": False,
                      "new_slot_start": datetime.now(timezone.utc).isoformat()},
            )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_cancel_booking_writes_audit_event():
    appt_id = uuid4()
    with patch("booking_engine.api.routes.voice_tools_booking.cancel_appointment",
               new=AsyncMock(return_value=True)), \
         patch("booking_engine.api.routes.voice_tools_booking.log_auth_event",
               new=AsyncMock(return_value=None)) as audit:
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/cancel_booking",
                headers=AUTH,
                json={"appointment_id": str(appt_id),
                      "verification_passed": True},
            )
    body = r.json()
    assert body["ok"] is True
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_booking_logs_failed_verification():
    appt_id = uuid4()
    with patch("booking_engine.api.routes.voice_tools_booking.log_auth_event",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/cancel_booking",
                headers=AUTH,
                json={"appointment_id": str(appt_id),
                      "verification_passed": False},
            )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "unauthorized"