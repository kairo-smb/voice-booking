"""Live DB tests — security-critical paths of the real dispatch chain:
token integrity, cross-shop authz, phone-mismatch authz, and constraint
enforcement, all proven against real rows (not mocks).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from booking_engine.db import connection
from booking_engine.db.queries import create_appointment, create_customer
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.mcp_tools import execute_tool
from tests.live_db.conftest import SHOP_ID, SHOP_ID_2, STAFF_MIRCO, SVC_TAGLIO_UOMO


def _next_weekday(offset_days: int) -> date:
    d = date.today() + timedelta(days=offset_days)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


def _slot(offset_days: int, hour: int = 11) -> datetime:
    """A Mon-Sat slot far enough out to be outside any cancellation lead time.
    Callers below use offsets spaced at least 2 raw days apart (60/62/64/66)
    — the Sunday-skip adjustment above only ever shifts a date forward by at
    most 1 day, so a 2-day raw gap between offsets is always collision-free
    regardless of what day "today" is (a 1-day gap is NOT safe: if one offset
    lands on Sunday and bumps forward, it can land on the exact same date as
    a neighboring offset one day later). This also keeps clear of the other
    live_db suites' +30..+54 day windows."""
    return datetime.combine(_next_weekday(offset_days), datetime.min.time().replace(hour=hour),
                            tzinfo=timezone.utc)


def _token(shop_id, call_id, settings) -> str:
    return mint_call_token(shop_id=shop_id, call_id=call_id, secret=settings.openai_tool_secret)


async def test_tampered_token_rejected(db_connection, tool_app, settings, cleanup_call_ids):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    tampered = token[:-1] + ("x" if token[-1] != "x" else "y")

    resp = await execute_tool(
        "get_services", {}, token=tampered, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "unauthorized"}


async def test_token_signed_with_wrong_secret_rejected(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    wrong_token = mint_call_token(shop_id=SHOP_ID, call_id=call_id, secret="not-the-real-secret")

    resp = await execute_tool(
        "get_services", {}, token=wrong_token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "unauthorized"}


async def test_unknown_tool_name_rejected(db_connection, tool_app, settings, cleanup_call_ids):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "drop_all_tables", {}, token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp == {"ok": False, "error": "unknown_tool"}


async def test_modify_booking_rejects_call_from_different_shop(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990010"
    customer = await create_customer(SHOP_ID, "Dispatch CrossShopModify", phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(60)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    # The call is recorded against SHOP_ID_2 — a different shop than the appointment.
    call_id = await insert_call(shop_id=SHOP_ID_2, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID_2, call_id, settings)

    resp = await execute_tool(
        "modify_booking",
        {"appointment_id": str(appt["id"]),
         "new_slot_start": (start + timedelta(hours=2)).isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is False
    assert resp["error"] == "wrong_shop"
    row = await connection.execute_one(
        "SELECT start_time FROM business_app_core.appointments WHERE id = $1", appt["id"],
    )
    assert abs((row["start_time"] - start).total_seconds()) < 1


async def test_cancel_booking_rejects_call_from_different_shop(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990011"
    customer = await create_customer(SHOP_ID, "Dispatch CrossShopCancel", phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(62)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID_2, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID_2, call_id, settings)

    resp = await execute_tool(
        "cancel_booking", {"appointment_id": str(appt["id"])},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is False
    assert resp["error"] == "wrong_shop"
    row = await connection.execute_one(
        "SELECT status FROM business_app_core.appointments WHERE id = $1", appt["id"],
    )
    assert row["status"] == "scheduled"


async def test_modify_booking_rejects_phone_mismatch(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    owner_phone = "+39 333 9990012"
    caller_phone = "+39 333 9990013"  # a different number calling in
    customer = await create_customer(SHOP_ID, "Dispatch PhoneMismatch", owner_phone)
    cleanup_customer_ids.append(customer["id"])
    start = _slot(64)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=caller_phone,
                                matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "modify_booking",
        {"appointment_id": str(appt["id"]),
         "new_slot_start": (start + timedelta(hours=2)).isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is False
    assert resp["error"] == "phone_mismatch"


async def test_modify_booking_rejects_within_lead_time(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990014"
    customer = await create_customer(SHOP_ID, "Dispatch LeadTimeModify", phone)
    cleanup_customer_ids.append(customer["id"])
    # 1 hour out — inside the default 2-hour cancellation lead time.
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    appt = await create_appointment(SHOP_ID, customer["id"], STAFF_MIRCO,
                                    [SVC_TAGLIO_UOMO], start)
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=phone,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "modify_booking",
        {"appointment_id": str(appt["id"]),
         "new_slot_start": (start + timedelta(hours=3)).isoformat()},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is False
    assert resp["error"] == "reschedule_too_close"


async def test_cancel_booking_rejects_within_lead_time(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    phone = "+39 333 9990015"
    customer = await create_customer(SHOP_ID, "Dispatch LeadTimeCancel", phone)
    cleanup_customer_ids.append(customer["id"])
    start = datetime.now(timezone.utc) + timedelta(hours=1)
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

    assert resp["ok"] is False
    assert resp["error"] == "cancel_too_close"


async def test_create_booking_rejects_slot_in_past(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
):
    customer = await create_customer(SHOP_ID, "Dispatch PastSlot")
    cleanup_customer_ids.append(customer["id"])
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None,
                                matched_customer_id=customer["id"])
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    past = datetime.now(timezone.utc) - timedelta(days=1)

    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(customer["id"]), "service_id": str(SVC_TAGLIO_UOMO),
         "slot_start": past.isoformat(), "staff_id": str(STAFF_MIRCO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is False
    assert resp["error"] == "slot_in_past"


async def test_create_booking_nonexistent_customer_returns_clean_error(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    nonexistent_customer_id = uuid4()

    resp = await execute_tool(
        "create_booking",
        {"customer_id": str(nonexistent_customer_id), "service_id": str(SVC_TAGLIO_UOMO),
         "slot_start": _slot(66).isoformat(), "staff_id": str(STAFF_MIRCO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    # customer_id is well-formed but references no real row — this reaches
    # create_appointment's raw INSERT (booking_engine/db/queries.py).
    # insert_booking_locked catches the resulting FK violation and
    # translates it to a clean envelope error (see voice_tool_queries.py).
    assert resp["ok"] is False
    assert resp["error"] == "invalid_customer"
