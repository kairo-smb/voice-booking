"""Tests for /voice/tools/* identity endpoints."""
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
    monkeypatch.setenv("VOICE_AGENT_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_lookup_customer_returns_matches():
    cid = uuid4()
    fake = [{"id": cid, "first_name": "Maria", "last_name": "Rossi",
             "last_visit_at": None, "preferred_staff_id": None,
             "notes_tags": [], "verified": True}]
    with patch("booking_engine.api.routes.voice_tools_identity.find_customers_by_phone",
               new=AsyncMock(return_value=fake)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/lookup_customer",
                headers=AUTH, json={"phone": "+393201234567"},
            )
    body = r.json()
    assert body["ok"] is True
    assert len(body["data"]) == 1
    assert body["data"][0]["customer_id"] == str(cid)


@pytest.mark.asyncio
async def test_create_customer_writes_row():
    new_id = uuid4()
    with patch("booking_engine.api.routes.voice_tools_identity.insert_customer_from_call",
               new=AsyncMock(return_value=new_id)), \
         patch("booking_engine.api.routes.voice_tools_identity.attach_customer_to_call",
               new=AsyncMock(return_value=None)):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/create_customer_from_call",
                headers=AUTH,
                json={"phone": "+393201234567", "first_name": "Marco",
                      "last_name": "Bianchi", "phone_source": "caller_id"},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["customer_id"] == str(new_id)


@pytest.mark.asyncio
async def test_update_customer_rejects_unknown_field():
    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/voice/tools/update_customer_from_call",
            headers=AUTH,
            json={"customer_id": str(uuid4()), "field": "phone", "value": "X"},
        )
    assert r.status_code == 422  # Pydantic Literal rejects


# ── shop ownership (the hole flagged 2026-07-17) ──────────────────────────
#
# The check reads the shop off the CALL ROW, not off the X-Shop-Id header —
# the same trust boundary as booking_authz.authorize_booking_change.

async def _update(customer_id, *, call, customer_shop, update=None):
    """Drive the route with get_call / get_customer_shop_id / the update stubbed."""
    mod = "booking_engine.api.routes.voice_tools_identity."
    with patch(mod + "get_call", new=AsyncMock(return_value=call)), \
         patch(mod + "get_customer_shop_id",
               new=AsyncMock(return_value=customer_shop)), \
         patch(mod + "update_customer_field",
               new=update or AsyncMock(return_value=True)) as upd:
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/update_customer_from_call",
                headers=AUTH,
                json={"customer_id": str(customer_id), "field": "email",
                      "value": "x@example.com"},
            )
        return r.json(), upd


@pytest.mark.asyncio
async def test_update_customer_refuses_another_shops_customer():
    shop_a, shop_b = uuid4(), uuid4()
    cid = uuid4()
    body, upd = await _update(
        cid, call={"id": uuid4(), "shop_id": shop_a, "caller_number": None},
        customer_shop=shop_b,
    )
    assert body == {"ok": False, "data": None, "error": "wrong_shop"}
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_customer_allows_its_own_shops_customer():
    shop_a = uuid4()
    cid = uuid4()
    body, upd = await _update(
        cid, call={"id": uuid4(), "shop_id": shop_a, "caller_number": None},
        customer_shop=shop_a,
    )
    assert body["ok"] is True
    assert body["data"] == {"updated": True}
    upd.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_customer_unknown_customer_is_a_named_error():
    body, upd = await _update(
        uuid4(), call={"id": uuid4(), "shop_id": uuid4(), "caller_number": None},
        customer_shop=None,
    )
    assert body == {"ok": False, "data": None, "error": "customer_not_found"}
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_customer_without_a_call_row_is_refused():
    body, upd = await _update(uuid4(), call=None, customer_shop=uuid4())
    assert body == {"ok": False, "data": None, "error": "call_not_found"}
    upd.assert_not_awaited()