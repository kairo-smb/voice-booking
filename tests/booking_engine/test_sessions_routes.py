"""Session-owned writes for the customer agents: `/sessions/{call_id}/*`.

Hermetic — the DB functions are stubbed at the route module. The property
asserted hardest is the scoping: every route answers 404 `unknown_session`
unless the `voice_agent.calls` row belongs to the `X-Shop-Id` shop, because the
caller (marketing-engine) is trusted with the token but the session id travels
through a model's context and must never reach another salon's row.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from booking_engine.api.app import create_app

_app = create_app()
_MOD = "booking_engine.api.routes.sessions"

SHOP = uuid4()
CALL = uuid4()
AUTH = {"Authorization": "Bearer tool-secret", "X-Shop-Id": str(SHOP)}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_TOOL_SECRET", "tool-secret")


def _call(shop_id=SHOP, **extra):
    return {"id": CALL, "shop_id": shop_id, "customer_id": None,
            "caller_number": "+393201234567", **extra}


async def _post(path, json, headers=AUTH):
    async with AsyncClient(transport=ASGITransport(app=_app),
                           base_url="http://t") as c:
        return await c.post(f"/sessions/{CALL}/{path}", headers=headers, json=json)


# ── scoping ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("customer", {"customer_id": str(uuid4())}),
    ("escalation", {"reason": "r", "customer_message": "m"}),
    ("outcome", {"outcome": "booked"}),
])
async def test_session_of_another_shop_is_unknown(path, body):
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call(uuid4()))), \
         patch(f"{_MOD}.set_call_outcome", new=AsyncMock()) as outcome, \
         patch(f"{_MOD}.insert_callback_memo", new=AsyncMock()) as memo, \
         patch(f"{_MOD}.link_customer", new=AsyncMock()) as link:
        r = await _post(path, body)
    assert r.status_code == 404
    assert r.json() == {"ok": False, "data": None, "error": "unknown_session"}
    outcome.assert_not_awaited()
    memo.assert_not_awaited()
    link.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_session_is_unknown():
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=None)):
        r = await _post("outcome", {"outcome": "booked"})
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_session"


@pytest.mark.asyncio
async def test_requires_the_tool_token():
    r = await _post("outcome", {"outcome": "booked"},
                    headers={"X-Shop-Id": str(SHOP)})
    assert r.status_code == 401


# ── customer ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_customer_links_the_session():
    customer = uuid4()
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.get_customer_shop_id", new=AsyncMock(return_value=SHOP)), \
         patch(f"{_MOD}.link_customer", new=AsyncMock(return_value={})) as link:
        r = await _post("customer", {"customer_id": str(customer)})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    link.assert_awaited_once_with(SHOP, CALL, customer)


@pytest.mark.asyncio
async def test_customer_of_another_shop_is_refused():
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.get_customer_shop_id",
               new=AsyncMock(return_value=uuid4())), \
         patch(f"{_MOD}.link_customer", new=AsyncMock()) as link:
        r = await _post("customer", {"customer_id": str(uuid4())})
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_customer"
    link.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonexistent_customer_is_refused():
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.get_customer_shop_id", new=AsyncMock(return_value=None)), \
         patch(f"{_MOD}.link_customer", new=AsyncMock()) as link:
        r = await _post("customer", {"customer_id": str(uuid4())})
    assert r.status_code == 404
    link.assert_not_awaited()


# ── escalation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_escalation_writes_memo_marks_session_and_pushes():
    memo_id = uuid4()
    customer = uuid4()
    insert = AsyncMock(return_value=memo_id)
    with patch(f"{_MOD}.get_call",
               new=AsyncMock(return_value=_call(customer_id=customer))), \
         patch(f"{_MOD}.insert_callback_memo", new=insert), \
         patch(f"{_MOD}.set_call_outcome", new=AsyncMock()) as outcome, \
         patch(f"{_MOD}.send_push", new=AsyncMock()) as push:
        r = await _post("escalation", {
            "reason": "vuole parlare con Giulia",
            "customer_message": "Vorrebbe cambiare data.",
            "callback_window": "oggi pomeriggio",
        })
    body = r.json()
    assert r.status_code == 200
    assert body == {"ok": True, "data": {"memo_id": str(memo_id)}, "error": None}
    kw = insert.await_args.kwargs
    assert kw["call_id"] == CALL and kw["shop_id"] == SHOP
    assert kw["customer_id"] == customer
    assert kw["caller_phone"] == "+393201234567"
    assert kw["reason"] == "vuole parlare con Giulia — Vorrebbe cambiare data."
    # the session's escalated flag *is* outcome='escalated' (wa_session_queries)
    assert outcome.await_args.kwargs["outcome"] == "escalated"
    assert push.await_args.kwargs["event"] == "voice_new_memo"


# ── outcome ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outcome_is_recorded():
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.set_call_outcome", new=AsyncMock()) as outcome:
        r = await _post("outcome", {"outcome": "booked",
                                    "summary": "Maria ha prenotato."})
    assert r.json() == {"ok": True, "data": {"marked": True}, "error": None}
    kw = outcome.await_args.kwargs
    assert kw["call_id"] == CALL
    assert kw["outcome"] == "booked" and kw["summary"] == "Maria ha prenotato."


@pytest.mark.asyncio
async def test_outcome_info_only_is_stored_as_info():
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.set_call_outcome", new=AsyncMock()) as outcome:
        r = await _post("outcome", {"outcome": "info_only"})
    assert r.status_code == 200
    assert outcome.await_args.kwargs["outcome"] == "info"


@pytest.mark.asyncio
async def test_outcome_outside_the_check_is_rejected():
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.set_call_outcome", new=AsyncMock()) as outcome:
        r = await _post("outcome", {"outcome": "whatever"})
    assert r.status_code == 422
    outcome.assert_not_awaited()


# ── outcome: the appointment it produced ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_name,created", [
    ("booked", True), ("rescheduled", False), ("cancelled", False),
])
async def test_outcome_records_the_sessions_appointment(outcome_name, created):
    appt = uuid4()
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.get_appointment_shop_id",
               new=AsyncMock(return_value=SHOP)) as owner, \
         patch(f"{_MOD}.set_call_outcome", new=AsyncMock()) as outcome, \
         patch(f"{_MOD}.attach_appointment_to_call", new=AsyncMock()) as attach:
        r = await _post("outcome", {"outcome": outcome_name,
                                    "appointment_id": str(appt)})
    assert r.json() == {"ok": True, "data": {"marked": True}, "error": None}
    assert owner.await_args.kwargs["appointment_id"] == appt
    outcome.assert_awaited_once()
    assert attach.await_args.kwargs == {
        "call_id": CALL, "appointment_id": appt, "created": created,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_shop", [uuid4(), None])
async def test_outcome_with_another_shops_appointment_is_refused(owner_shop):
    # "Not yours" and "not there" are one refusal, like the session check.
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.get_appointment_shop_id",
               new=AsyncMock(return_value=owner_shop)), \
         patch(f"{_MOD}.set_call_outcome", new=AsyncMock()) as outcome, \
         patch(f"{_MOD}.attach_appointment_to_call", new=AsyncMock()) as attach:
        r = await _post("outcome", {"outcome": "booked",
                                    "appointment_id": str(uuid4())})
    assert r.status_code == 404
    assert r.json() == {"ok": False, "data": None, "error": "unknown_appointment"}
    # A refusal writes nothing — not even the outcome.
    outcome.assert_not_awaited()
    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_outcome_without_appointment_touches_no_appointment():
    with patch(f"{_MOD}.get_call", new=AsyncMock(return_value=_call())), \
         patch(f"{_MOD}.get_appointment_shop_id", new=AsyncMock()) as owner, \
         patch(f"{_MOD}.set_call_outcome", new=AsyncMock()), \
         patch(f"{_MOD}.attach_appointment_to_call", new=AsyncMock()) as attach:
        r = await _post("outcome", {"outcome": "info_only"})
    assert r.status_code == 200
    owner.assert_not_awaited()
    attach.assert_not_awaited()
