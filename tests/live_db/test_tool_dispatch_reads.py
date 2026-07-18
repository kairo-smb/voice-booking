"""Live DB tests — read tools via the real dispatch path (execute_tool() →
route handler → queries), against real Neon-shaped data.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from booking_engine.db.queries import create_appointment, create_customer
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.mcp_tools import execute_tool
from tests.live_db.conftest import (
    CUSTOMER_MARIA, PHONE_MARIA, SHOP_ID, STAFF_MIRCO, SVC_TAGLIO_UOMO,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for live-DB tests",
)


def _next_weekday() -> date:
    """A date 45+ days out that's Mon-Sat — isolated from the other live_db
    suites' own booking windows (they use +30..+33 days) so availability and
    booking assertions here never collide with theirs."""
    d = date.today() + timedelta(days=45)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


def _token(shop_id, call_id, settings) -> str:
    return mint_call_token(shop_id=shop_id, call_id=call_id, secret=settings.openai_tool_secret)


async def test_lookup_customer_returns_seeded_customer(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(
        shop_id=SHOP_ID, caller_phone=PHONE_MARIA, matched_customer_id=CUSTOMER_MARIA,
    )
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "lookup_customer", {"phone": PHONE_MARIA},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert any(c["customer_id"] == str(CUSTOMER_MARIA) for c in resp["data"])


async def test_get_services_returns_seeded_service(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "get_services", {}, token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert any(s["service_id"] == str(SVC_TAGLIO_UOMO) for s in resp["data"])


async def test_get_staff_for_service_returns_seeded_staff(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "get_staff_for_service", {"service_id": str(SVC_TAGLIO_UOMO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert any(s["staff_id"] == str(STAFF_MIRCO) for s in resp["data"])


async def test_check_availability_returns_slots(
    db_connection, tool_app, settings, cleanup_call_ids,
):
    call_id = await insert_call(shop_id=SHOP_ID, caller_phone=None, matched_customer_id=None)
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)
    preferred = datetime.combine(_next_weekday(), datetime.min.time(), tzinfo=timezone.utc)

    resp = await execute_tool(
        "check_availability",
        {"service_id": str(SVC_TAGLIO_UOMO), "preferred_when": preferred.isoformat(),
         "staff_id": str(STAFF_MIRCO)},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert len(resp["data"]) > 0
    assert resp["data"][0]["staff_id"] == str(STAFF_MIRCO)


async def test_get_booking_returns_customers_next_appointment(
    db_connection, tool_app, settings, cleanup_call_ids, cleanup_customer_ids,
    cleanup_appointment_ids,
):
    customer = await create_customer(SHOP_ID, "Dispatch GetBookingTest")
    cleanup_customer_ids.append(customer["id"])
    start = datetime.combine(_next_weekday(), datetime.min.time().replace(hour=11),
                             tzinfo=timezone.utc)
    appt = await create_appointment(
        SHOP_ID, customer["id"], STAFF_MIRCO, [SVC_TAGLIO_UOMO], start,
    )
    cleanup_appointment_ids.append(appt["id"])
    call_id = await insert_call(
        shop_id=SHOP_ID, caller_phone=None, matched_customer_id=customer["id"],
    )
    cleanup_call_ids.append(call_id)
    token = _token(SHOP_ID, call_id, settings)

    resp = await execute_tool(
        "get_booking", {"customer_id": str(customer["id"])},
        token=token, secret=settings.openai_tool_secret, app=tool_app,
    )

    assert resp["ok"] is True
    assert resp["data"]["id"] == str(appt["id"])
