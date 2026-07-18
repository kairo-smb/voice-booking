"""Live DB tests — write tools via the real dispatch path (execute_tool() →
route handler → safety_layer authz/constraints → queries), against real
Neon-shaped data. Every assertion re-reads the row directly, not just the
tool's return value.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from booking_engine.db import connection
from booking_engine.db.queries import create_appointment, create_customer
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.mcp_tools import execute_tool
from tests.live_db.conftest import SHOP_ID, STAFF_MIRCO, SVC_TAGLIO_UOMO


def _next_weekday(offset_days: int) -> date:
    """A Mon-Sat date `offset_days` out. Callers below space their offsets
    (50/51/52) apart so their created appointments never collide with each
    other, with test_tool_dispatch_reads.py's +45 day window, or with the
    other live_db suites' own +0..+33 day windows."""
    d = date.today() + timedelta(days=offset_days)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


def _slot(offset_days: int, hour: int = 11) -> datetime:
    return datetime.combine(_next_weekday(offset_days), datetime.min.time().replace(hour=hour),
                            tzinfo=timezone.utc)


def _token(shop_id, call_id, settings) -> str:
    return mint_call_token(shop_id=shop_id, call_id=call_id, secret=settings.openai_tool_secret)


async def test_create_customer_from_call_persists_row(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone="+39 333 9990001",
                                matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "create_customer_from_call",
        {"phone": "+39 333 9990001", "first_name": "Dispatch", "last_name": "Test",
         "phone_source": "caller_id"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    new_id = resp["data"]["customer_id"]
    cleanup_customer_ids.append(new_id)

    row = await connection.execute_one(
        "SELECT full_name, phone_verified FROM business_app_core.customers WHERE id = $1",
        new_id,
    )
    assert row["full_name"] == "Dispatch Test"
    assert row["phone_verified"] is True

    call_row = await connection.execute_one(
        "SELECT created_customer_id FROM voice_agent.calls WHERE id = $1", call_id,
    )
    assert str(call_row["created_customer_id"]) == str(new_id)


async def test_update_customer_from_call_persists_change(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
):
    customer = await create_customer(SHOP_ID, "Dispatch UpdateTest")
    cleanup_customer_ids.append(customer["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "update_customer_from_call",
        {"customer_id": str(customer["id"]), "field": "email",
         "value": "dispatch-test@example.com"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    row = await connection.execute_one(
        "SELECT email FROM business_app_core.customers WHERE id = $1", customer["id"],
    )
    assert row["email"] == "dispatch-test@example.com"


async def test_create_booking_persists_appointment(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    customer = await create_customer(SHOP_ID, "Dispatch CreateBooking")
    cleanup_customer_ids.append(customer["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    start = _slot(50)

    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(customer["id"]), "service_id": str(SVC_TAGLIO_UOMO),
         "slot_start": start.isoformat(), "staff_id": str(STAFF_MIRCO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    appt_id = resp["data"]["appointment_id"]
    cleanup_appointment_ids.append(appt_id)

    row = await connection.execute_one(
        "SELECT status, voice_call_id FROM business_app_core.appointments WHERE id = $1",
        appt_id,
    )
    assert row["status"] == "scheduled"
    assert str(row["voice_call_id"]) == str(call_id)


async def test_modify_booking_authorized_changes_slot(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990002"
    customer = await create_customer(SHOP_ID, "Dispatch ModifyTest", phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(51)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    new_start = start + timedelta(hours=2)

    resp = await execute_tool(
        "modify_booking",
        {"appointment_id": str(appt["id"]), "new_slot_start": new_start.isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    row = await connection.execute_one(
        "SELECT start_time FROM business_app_core.appointments WHERE id = $1", appt["id"],
    )
    assert abs((row["start_time"] - new_start).total_seconds()) < 1


async def test_cancel_booking_authorized_cancels(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990003"
    customer = await create_customer(SHOP_ID, "Dispatch CancelTest", phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(52)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "cancel_booking", {"appointment_id": str(appt["id"])},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    row = await connection.execute_one(
        "SELECT status FROM business_app_core.appointments WHERE id = $1", appt["id"],
    )
    assert row["status"] == "cancelled"


async def test_mark_outcome_persists_on_call_row(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "mark_outcome",
        {"outcome": "info", "summary": "Dispatch test summary"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    row = await connection.execute_one(
        "SELECT outcome, summary FROM voice_agent.calls WHERE id = $1", call_id,
    )
    assert row["outcome"] == "info"
    assert row["summary"] == "Dispatch test summary"


async def test_escalate_to_merchant_creates_memo(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_memo_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone="+39 333 9990004",
                                matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "escalate_to_merchant",
        {"reason": "vuole parlare con il salone",
         "customer_message": "Richiamatemi per favore"},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    memo_id = resp["data"]["memo_id"]
    cleanup_memo_ids.append(memo_id)

    row = await connection.execute_one(
        "SELECT reason, caller_phone FROM voice_agent.callback_memos WHERE id = $1", memo_id,
    )
    assert "vuole parlare con il salone" in row["reason"]
    assert row["caller_phone"] == "+39 333 9990004"

    call_row = await connection.execute_one(
        "SELECT outcome FROM voice_agent.calls WHERE id = $1", call_id,
    )
    assert call_row["outcome"] == "escalated"
