"""find_availability ranks by the hour asked for, not by the start of the day."""
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from booking_engine.db import queries
from booking_engine.db import voice_tool_queries as vtq

ROME = ZoneInfo("Europe/Rome")
DAY = datetime(2026, 10, 1, tzinfo=ROME)


def _slots():
    # An empty Thursday, 09:00-18:00 every 30 min, one stylist.
    staff = uuid4()
    out = []
    t = DAY.replace(hour=9)
    while t.hour < 18:
        out.append({"slot_start": t, "slot_end": t + timedelta(minutes=30),
                    "staff_id": staff, "staff_name": "Marco"})
        t += timedelta(minutes=30)
    return out


@pytest.mark.asyncio
async def test_the_slots_returned_are_the_ones_nearest_the_requested_hour(monkeypatch):
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return _slots()
    monkeypatch.setattr(queries, "get_available_slots", fake)

    # Naive = salon time, as a customer says "giovedì verso le 15".
    rows = await vtq.find_availability(
        shop_id=uuid4(), services=[{"service_id": uuid4()}],
        preferred_when=datetime(2026, 10, 1, 15, 0), max_results=5,
    )
    starts = [r["slot_start"].strftime("%H:%M") for r in rows]
    assert starts == ["14:00", "14:30", "15:00", "15:30", "16:00"]
    assert seen["start_date"] == DAY.date()


@pytest.mark.asyncio
async def test_without_a_preference_it_is_still_the_earliest(monkeypatch):
    async def fake(**kw):
        return _slots()
    monkeypatch.setattr(queries, "get_available_slots", fake)
    rows = await vtq.find_availability(
        shop_id=uuid4(), services=[{"service_id": uuid4()}],
        preferred_when=None, max_results=2,
    )
    assert [r["slot_start"].hour for r in rows] == [9, 9]
