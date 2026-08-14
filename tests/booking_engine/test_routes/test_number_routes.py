"""Tests for POST /voice/numbers/request, GET /voice/numbers/request/{shop_id},
POST /messaging/tick, and the idempotent POST /voice/numbers/provision path.
"""
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
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token123")


@pytest.mark.asyncio
async def test_request_number_requires_auth():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/api/v1/voice/numbers/request",
            data={
                "shop_id": str(uuid4()),
                "business_name": "Salone Bella",
                "contact_email": "a@b.com",
            },
            files={"document": ("doc.pdf", b"fake-bytes", "application/pdf")},
        )
        assert r.status_code in (401, 403, 503)


@pytest.mark.asyncio
async def test_tick_requires_auth():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/v1/messaging/tick")
        assert r.status_code in (401, 403, 503)


@pytest.mark.asyncio
async def test_request_number_rejects_blank_business_name():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/api/v1/voice/numbers/request",
            headers=AUTH,
            data={
                "shop_id": str(uuid4()),
                "business_name": "",
                "contact_email": "a@b.com",
            },
            files={"document": ("doc.pdf", b"fake-bytes", "application/pdf")},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_request_number_calls_submit_request():
    shop_id = uuid4()
    with patch(
        "booking_engine.api.routes.voice_telephony.submit_request",
        new_callable=AsyncMock,
        return_value={"ok": True, "status": "pending_review"},
    ) as mock_submit:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/numbers/request",
                headers=AUTH,
                data={
                    "shop_id": str(shop_id),
                    "business_name": "Salone Bella",
                    "contact_email": "a@b.com",
                },
                files={"document": ("doc.pdf", b"fake-bytes", "application/pdf")},
            )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "pending_review"
        mock_submit.assert_called_once()
        assert mock_submit.call_args.kwargs["shop_id"] == shop_id
        assert mock_submit.call_args.kwargs["business_name"] == "Salone Bella"
        assert mock_submit.call_args.kwargs["contact_email"] == "a@b.com"
        assert mock_submit.call_args.kwargs["filename"] == "doc.pdf"
        assert mock_submit.call_args.kwargs["content"] == b"fake-bytes"


@pytest.mark.asyncio
async def test_get_request_status_returns_request_and_telephony():
    shop_id = uuid4()
    request_row = {
        "shop_id": shop_id,
        "status": "pending_review",
        "regulation_sid": "RN1",
        "bundle_sid": "BU1",
        "end_user_sid": "EU1",
        "document_sid": "RD1",
        "business_name": "Salone Bella",
        "contact_email": "a@b.com",
        "evaluation_errors": None,
        "rejection_reason": None,
        "created_at": None,
        "submitted_at": None,
        "reviewed_at": None,
        "updated_at": None,
    }
    with patch(
        "booking_engine.api.routes.voice_telephony.rq.get_request",
        new_callable=AsyncMock,
        return_value=request_row,
    ), patch(
        "booking_engine.api.routes.voice_telephony.q.get_telephony",
        new_callable=AsyncMock,
        return_value=None,
    ):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(
                f"/api/v1/voice/numbers/request/{shop_id}", headers=AUTH
            )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["request"]["status"] == "pending_review"
        assert data["telephony"] is None


@pytest.mark.asyncio
async def test_get_request_status_none_when_nothing_exists():
    shop_id = uuid4()
    with patch(
        "booking_engine.api.routes.voice_telephony.rq.get_request",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "booking_engine.api.routes.voice_telephony.q.get_telephony",
        new_callable=AsyncMock,
        return_value=None,
    ):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(
                f"/api/v1/voice/numbers/request/{shop_id}", headers=AUTH
            )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["request"] is None
        assert data["telephony"] is None


@pytest.mark.asyncio
async def test_provision_already_provisioned_returns_existing_and_skips_purchase():
    shop_id = uuid4()
    existing_row = {
        "shop_id": shop_id,
        "kairo_number": "+37251234567",
        "kairo_number_sid": "PN1",
        "setup_path": "new",
        "salon_existing_number": None,
    }
    with patch(
        "booking_engine.api.routes.voice_telephony.q.get_telephony",
        new_callable=AsyncMock,
        return_value=existing_row,
    ), patch(
        "booking_engine.api.routes.voice_telephony.purchase_number"
    ) as mock_purchase:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/voice/numbers/provision",
                headers=AUTH,
                json={
                    "shop_id": str(shop_id),
                    "phone_number": "+37259999999",
                    "setup_path": "new",
                },
            )
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["kairo_number"] == "+37251234567"
        mock_purchase.assert_not_called()


@pytest.mark.asyncio
async def test_tick_with_approved_bundle_calls_provision_approved():
    shop_id = uuid4()
    with patch(
        "booking_engine.api.routes.messaging_tick.list_pending_review",
        new_callable=AsyncMock,
        return_value=[{"shop_id": shop_id, "bundle_sid": "BU1"}],
    ), patch(
        "booking_engine.api.routes.messaging_tick.get_bundle_status",
        new_callable=AsyncMock,
        return_value="twilio-approved",
    ), patch(
        "booking_engine.api.routes.messaging_tick.set_status",
        new_callable=AsyncMock,
    ), patch(
        "booking_engine.api.routes.messaging_tick.provision_approved",
        new_callable=AsyncMock,
        return_value="provisioned",
    ) as mock_provision, patch(
        "booking_engine.api.routes.messaging_tick.check_all",
        new_callable=AsyncMock,
        return_value={"checked": 0, "green": 0, "red": 0, "inconclusive": 0},
    ):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/messaging/tick", headers=AUTH)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["reviewed"] == 1
        assert data["provisioned"] == 1
        assert data["errors"] == 0
        mock_provision.assert_called_once()
        assert mock_provision.call_args.args[0] == shop_id


@pytest.mark.asyncio
async def test_tick_one_bad_shop_does_not_stop_the_sweep_or_health_check():
    good_shop = uuid4()
    bad_shop = uuid4()

    async def fake_get_bundle_status(*, bundle_sid, account_sid, auth_token):
        if bundle_sid == "BAD":
            raise RuntimeError("twilio blew up")
        return "twilio-approved"

    with patch(
        "booking_engine.api.routes.messaging_tick.list_pending_review",
        new_callable=AsyncMock,
        return_value=[
            {"shop_id": bad_shop, "bundle_sid": "BAD"},
            {"shop_id": good_shop, "bundle_sid": "GOOD"},
        ],
    ), patch(
        "booking_engine.api.routes.messaging_tick.get_bundle_status",
        side_effect=fake_get_bundle_status,
    ), patch(
        "booking_engine.api.routes.messaging_tick.set_status",
        new_callable=AsyncMock,
    ), patch(
        "booking_engine.api.routes.messaging_tick.provision_approved",
        new_callable=AsyncMock,
        return_value="provisioned",
    ) as mock_provision, patch(
        "booking_engine.api.routes.messaging_tick.check_all",
        new_callable=AsyncMock,
        return_value={"checked": 3, "green": 3, "red": 0, "inconclusive": 0},
    ) as mock_check_all:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/messaging/tick", headers=AUTH)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["errors"] == 1
        assert data["provisioned"] == 1
        # The bad shop must not prevent the good shop's provisioning, and
        # health must still run for every shop regardless.
        mock_provision.assert_called_once()
        assert mock_provision.call_args.args[0] == good_shop
        mock_check_all.assert_called_once()
        assert data["health"]["checked"] == 3
