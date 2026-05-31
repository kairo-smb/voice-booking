"""Tests for CallSession lifecycle (DB mocked at module-function level)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from voice_gateway.call_lifecycle import CallSession


@pytest.mark.asyncio
async def test_start_inserts_call_row_with_existing_match():
    shop = uuid4()
    customer_id = uuid4()
    with (
        patch("voice_gateway.call_lifecycle.execute",
              AsyncMock(return_value=[{"id": customer_id}])) as ex,
        patch("voice_gateway.call_lifecycle.execute_void",
              AsyncMock()) as ev,
    ):
        sess = CallSession(shop_id=shop, caller_number="+390000", twilio_call_sid="CA1")
        await sess.start()
        ex.assert_awaited()
        ev.assert_awaited()
        assert sess.customer_match == "existing"
        assert sess.customer_id == customer_id
        assert sess.id is not None


@pytest.mark.asyncio
async def test_start_unmatched_when_no_phone_contact():
    with (
        patch("voice_gateway.call_lifecycle.execute",
              AsyncMock(return_value=[])),
        patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()),
    ):
        sess = CallSession(shop_id=uuid4(), caller_number="+390000", twilio_call_sid="CA2")
        await sess.start()
        assert sess.customer_match == "unmatched"
        assert sess.customer_id is None


@pytest.mark.asyncio
async def test_start_ambiguous_when_multiple_customers():
    cid1, cid2 = uuid4(), uuid4()
    with (
        patch("voice_gateway.call_lifecycle.execute",
              AsyncMock(return_value=[{"id": cid1}, {"id": cid2}])),
        patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()),
    ):
        sess = CallSession(shop_id=uuid4(), caller_number="+390000", twilio_call_sid="CA3")
        await sess.start()
        assert sess.customer_match == "ambiguous"
        assert sess.customer_id is None


@pytest.mark.asyncio
async def test_append_turn_writes_transcript():
    with patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()) as ev:
        sess = CallSession(shop_id=uuid4(), caller_number="+39",
                           twilio_call_sid=None)
        sess.id = uuid4()
        await sess.append_turn(role="assistant", text="Ciao",
                               at=datetime.now(timezone.utc))
        ev.assert_awaited_once()
        sql = ev.await_args.args[0]
        assert "call_transcripts" in sql


@pytest.mark.asyncio
async def test_log_event_writes_call_event():
    with patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()) as ev:
        sess = CallSession(shop_id=uuid4(), caller_number="+39",
                           twilio_call_sid=None)
        sess.id = uuid4()
        await sess.log_event("function_call", {"name": "book", "args": {}})
        sql = ev.await_args.args[0]
        assert "call_events" in sql


@pytest.mark.asyncio
async def test_set_appointment_remembers_id():
    sess = CallSession(shop_id=uuid4(), caller_number="+39", twilio_call_sid=None)
    aid = uuid4()
    sess.set_appointment(aid)
    assert sess.appointment_id == aid


@pytest.mark.asyncio
async def test_finalize_updates_call_with_outcome():
    classifier = AsyncMock(return_value={
        "outcome": "booked", "outcome_reason": "ok", "summary": "Maria booked",
    })
    with patch("voice_gateway.call_lifecycle.execute_void", AsyncMock()) as ev:
        sess = CallSession(shop_id=uuid4(), caller_number="+39", twilio_call_sid=None)
        sess.id = uuid4()
        sess.transcript = [{"role": "caller", "text": "Vorrei prenotare"}]
        sess.set_appointment(uuid4())
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        sess.started_at = _dt.now(_tz.utc) - _td(seconds=120)
        await sess.finalize(classifier=classifier, api_key="sk", model="gpt")
        classifier.assert_awaited_once()
        sql = ev.await_args.args[0]
        assert "UPDATE voice_agent.calls" in sql
