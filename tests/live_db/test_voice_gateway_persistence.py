"""End-to-end: simulate a call lifecycle and assert persistence in voice_agent.*"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from voice_gateway.db import init_pool, close_pool, execute, execute_one, execute_void
from voice_gateway.call_lifecycle import CallSession


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)


@pytest.fixture(autouse=True)
async def db():
    await init_pool(os.environ["DATABASE_URL"])
    yield
    await close_pool()


@pytest.fixture
async def shop(db):
    sid = uuid4()
    await execute_void(
        "INSERT INTO business_app_core.shops (id, name, is_active) "
        "VALUES ($1, 'TestShop-VG', true)",
        sid,
    )
    yield sid
    await execute_void("DELETE FROM voice_agent.calls WHERE shop_id = $1", sid)
    await execute_void("DELETE FROM business_app_core.shops WHERE id = $1", sid)


async def test_full_call_lifecycle_persists(shop):
    classifier = AsyncMock(return_value={
        "outcome": "booked", "outcome_reason": "ok", "summary": "Maria prenotata",
    })

    sess = CallSession(shop_id=shop, caller_number="+390000000",
                       twilio_call_sid=f"CA{uuid4().hex[:8]}")
    await sess.start()
    assert sess.customer_match == "unmatched"
    assert sess.id is not None

    now = datetime.now(timezone.utc)
    await sess.append_turn(role="assistant", text="Ciao", at=now)
    await sess.append_turn(role="caller", text="Vorrei prenotare", at=now)
    await sess.log_event("function_call", {"name": "book_appointment"})
    await sess.finalize(classifier=classifier, api_key="sk", model="gpt-4o-mini")

    row = await execute_one(
        "SELECT outcome, duration_seconds, summary "
        "FROM voice_agent.calls WHERE id = $1", sess.id,
    )
    assert row["outcome"] == "booked"
    assert row["duration_seconds"] is not None
    assert row["summary"] == "Maria prenotata"

    turns = await execute(
        "SELECT role, text FROM voice_agent.call_transcripts "
        "WHERE call_id = $1 ORDER BY turn_index", sess.id,
    )
    assert [t["role"] for t in turns] == ["assistant", "caller"]

    events = await execute(
        "SELECT type FROM voice_agent.call_events WHERE call_id = $1", sess.id,
    )
    assert any(e["type"] == "function_call" for e in events)


async def test_existing_phone_contact_yields_existing_match(shop):
    cust_id = uuid4()
    phone = f"+39{uuid4().int % 10**10}"
    await execute_void(
        "INSERT INTO business_app_core.customers (id, shop_id, full_name) "
        "VALUES ($1, $2, 'Mario')",
        cust_id, shop,
    )
    await execute_void(
        "INSERT INTO business_app_core.phone_contacts (phone_number, customer_id) "
        "VALUES ($1, $2)",
        phone, cust_id,
    )
    sess = CallSession(shop_id=shop, caller_number=phone, twilio_call_sid=None)
    await sess.start()
    assert sess.customer_match == "existing"
    assert sess.customer_id == cust_id
    # Clean calls referencing this customer before removing customer.
    await execute_void("DELETE FROM voice_agent.calls WHERE customer_id = $1", cust_id)
    await execute_void("DELETE FROM business_app_core.phone_contacts "
                       "WHERE phone_number = $1", phone)
    await execute_void("DELETE FROM business_app_core.customers WHERE id = $1", cust_id)
