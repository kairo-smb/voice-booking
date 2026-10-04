"""SQL shape of the two statements behind `/sessions/{id}/outcome`'s
`appointment_id`: which columns they write, and that a reschedule/cancel never
re-points the appointment's originating call."""
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from booking_engine.db import voice_calls_queries as q


@pytest.mark.asyncio
async def test_booked_sets_the_session_and_the_appointments_origin(monkeypatch):
    void = AsyncMock()
    monkeypatch.setattr(q.connection, "execute_void", void)
    call, appt = uuid4(), uuid4()

    await q.attach_appointment_to_call(call_id=call, appointment_id=appt, created=True)

    (calls_sql, *calls_args), _ = void.await_args_list[0]
    assert "UPDATE voice_agent.calls" in calls_sql
    assert "appointment_id = $2" in calls_sql and "created_booking_id" in calls_sql
    assert calls_args == [call, appt, True]
    (appt_sql, *appt_args), _ = void.await_args_list[1]
    assert "business_app_core.appointments" in appt_sql
    assert "voice_call_id IS NULL" in appt_sql
    assert appt_args == [call, appt]


@pytest.mark.asyncio
async def test_a_change_only_points_the_session_at_the_appointment(monkeypatch):
    void = AsyncMock()
    monkeypatch.setattr(q.connection, "execute_void", void)

    await q.attach_appointment_to_call(call_id=uuid4(), appointment_id=uuid4(),
                                       created=False)

    assert void.await_count == 1
    assert "business_app_core.appointments" not in void.await_args.args[0]


@pytest.mark.asyncio
async def test_appointment_shop_is_none_when_missing(monkeypatch):
    monkeypatch.setattr(q.connection, "execute_one", AsyncMock(return_value=None))
    assert await q.get_appointment_shop_id(appointment_id=uuid4()) is None
