"""Live DB tests — availability slot generation against real schedules.

Tests the get_available_slots function with real staff schedules, services,
and existing appointments in Neon PostgreSQL.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from booking_engine.db.queries import get_available_slots
from tests.live_db.conftest import (
    SHOP_ID,
    STAFF_MIRCO,
    STAFF_GIULIA,
    SVC_TAGLIO_DONNA,
    SVC_TAGLIO_UOMO,
    SVC_COLORE,
    SVC_PIEGA,
)


def _next_weekday() -> date:
    """Return a date 30+ days in the future that is Mon-Sat."""
    d = date.today() + timedelta(days=30)
    while d.weekday() == 6:  # skip Sunday
        d += timedelta(days=1)
    return d


class TestAvailableSlotsSingleService:
    async def test_taglio_uomo_returns_slots_on_weekday(self, db_connection):
        """Taglio uomo is 30 min. Mirco works 10:00-18:00.
        Should get 30-min slots throughout the day."""
        day = _next_weekday()
        slots = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO], day, day,
        )
        assert len(slots) > 0
        # All slots should be on the requested day
        for s in slots:
            assert str(day) in str(s["slot_start"])

    async def test_no_slots_on_sunday(self, db_connection):
        """Staff don't work Sundays — expect no slots."""
        d = date.today() + timedelta(days=30)
        while d.weekday() != 6:  # find a Sunday
            d += timedelta(days=1)
        slots = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO], d, d,
        )
        assert len(slots) == 0


class TestAvailableSlotsStaffFilter:
    async def test_filter_by_mirco(self, db_connection):
        """Only Mirco's slots when staff_id is specified."""
        day = _next_weekday()
        slots = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO], day, day,
            staff_id=STAFF_MIRCO,
        )
        assert len(slots) > 0
        assert all(s["staff_name"] == "Mirco Meazzo" for s in slots)

    async def test_giulia_cannot_do_taglio(self, db_connection):
        """Giulia doesn't do Taglio uomo — expect no slots for her."""
        day = _next_weekday()
        slots = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO], day, day,
            staff_id=STAFF_GIULIA,
        )
        assert len(slots) == 0


class TestAvailableSlotsMultiService:
    async def test_taglio_plus_piega_returns_longer_slots(self, db_connection):
        """Taglio uomo (30) + Piega (30) = 60 min total.
        Slots should be fewer than single-service because they need more time."""
        day = _next_weekday()

        single = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO], day, day,
            staff_id=STAFF_MIRCO,
        )
        multi = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO, SVC_PIEGA], day, day,
            staff_id=STAFF_MIRCO,
        )
        assert len(multi) > 0
        assert len(multi) < len(single)  # fewer slots for longer service

    async def test_services_across_different_staff_skills(self, db_connection):
        """Taglio uomo + Colore — Mirco can't do Colore, Giulia can't do Taglio.
        No single staff can do both, so expect no slots."""
        day = _next_weekday()
        slots = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO, SVC_COLORE], day, day,
        )
        assert len(slots) == 0


class TestAvailableSlotsDateRange:
    async def test_multi_day_range(self, db_connection):
        """Two weekdays should return more slots than one."""
        day1 = _next_weekday()
        day2 = day1 + timedelta(days=1)
        if day2.weekday() == 6:
            day2 += timedelta(days=1)

        one_day = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO], day1, day1,
            staff_id=STAFF_MIRCO,
        )
        two_days = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO], day1, day2,
            staff_id=STAFF_MIRCO,
        )
        assert len(two_days) > len(one_day)


class TestAvailableSlotsNonexistentService:
    async def test_fake_service_returns_empty(self, db_connection):
        """A nonexistent service ID should return no slots (duration = 0)."""
        from uuid import UUID
        day = _next_weekday()
        fake = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        slots = await get_available_slots(SHOP_ID, [fake], day, day)
        assert slots == []


class TestSlotOrdering:
    async def test_slots_sorted_by_time_then_staff(self, db_connection):
        """Slots should come back sorted by start time, then staff name."""
        day = _next_weekday()
        slots = await get_available_slots(
            SHOP_ID, [SVC_TAGLIO_UOMO], day, day,
        )
        if len(slots) >= 2:
            for i in range(len(slots) - 1):
                a, b = slots[i], slots[i + 1]
                assert (str(a["slot_start"]), a["staff_name"]) <= (str(b["slot_start"]), b["staff_name"])
