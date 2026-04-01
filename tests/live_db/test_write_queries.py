"""Live DB tests — write operations with cleanup.

Tests that create, modify, and delete records in the real Neon PostgreSQL
database. All test data is cleaned up after each test.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking_engine.db.queries import (
    SlotConflictError,
    create_customer,
    create_appointment,
    cancel_appointment,
    reschedule_appointment,
    list_appointments,
    find_customers_by_phone,
)
from tests.live_db.conftest import (
    SHOP_ID,
    STAFF_MIRCO,
    SVC_TAGLIO_UOMO,
    SVC_PIEGA,
)

ROME = ZoneInfo("Europe/Rome")


def _next_weekday_10am() -> datetime:
    """Return next Monday-Saturday at 10:00 Rome time (avoids schedule conflicts)."""
    now = datetime.now(tz=ROME)
    # Go far enough into the future to avoid existing appointments
    dt = now.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=30)
    # Make sure it's a weekday (Mon-Sat, day_of_week 0-5)
    while dt.weekday() == 6:  # skip Sunday
        dt += timedelta(days=1)
    return dt


class TestCreateCustomerLive:
    async def test_creates_customer_without_phone(self, db_connection, cleanup_customer_ids):
        customer = await create_customer(SHOP_ID, "Test User NoPhone")
        assert customer is not None
        assert customer["full_name"] == "Test User NoPhone"
        cleanup_customer_ids.append(customer["id"])

    async def test_creates_customer_with_phone(self, db_connection, cleanup_customer_ids):
        customer = await create_customer(SHOP_ID, "Test User WithPhone", "+39 999 0000001")
        assert customer is not None
        assert customer["full_name"] == "Test User WithPhone"
        cleanup_customer_ids.append(customer["id"])

        # Verify phone contact was created
        found = await find_customers_by_phone(SHOP_ID, "+39 999 0000001")
        assert len(found) >= 1
        assert any(c["id"] == customer["id"] for c in found)


class TestCreateAppointmentLive:
    async def test_creates_appointment_successfully(
        self, db_connection, cleanup_customer_ids, cleanup_appointment_ids
    ):
        # Create a test customer
        customer = await create_customer(SHOP_ID, "Test Appt Customer")
        cleanup_customer_ids.append(customer["id"])

        start = _next_weekday_10am()
        appt = await create_appointment(
            shop_id=SHOP_ID,
            customer_id=customer["id"],
            staff_id=STAFF_MIRCO,
            service_ids=[SVC_TAGLIO_UOMO],  # 30 min
            start_time=start,
            notes="Live DB test appointment",
        )
        assert appt is not None
        assert appt["status"] == "scheduled"
        cleanup_appointment_ids.append(appt["id"])

    async def test_raises_conflict_on_double_booking(
        self, db_connection, cleanup_customer_ids, cleanup_appointment_ids
    ):
        customer = await create_customer(SHOP_ID, "Test Conflict Customer")
        cleanup_customer_ids.append(customer["id"])

        start = _next_weekday_10am() + timedelta(hours=2)  # offset to avoid other tests

        # First booking succeeds
        appt1 = await create_appointment(
            SHOP_ID, customer["id"], STAFF_MIRCO,
            [SVC_TAGLIO_UOMO], start,
        )
        cleanup_appointment_ids.append(appt1["id"])

        # Same slot, same staff — should conflict
        with pytest.raises(SlotConflictError):
            appt2 = await create_appointment(
                SHOP_ID, customer["id"], STAFF_MIRCO,
                [SVC_TAGLIO_UOMO], start,
            )
            # If somehow created, clean up
            cleanup_appointment_ids.append(appt2["id"])

    async def test_multiple_services_extend_duration(
        self, db_connection, cleanup_customer_ids, cleanup_appointment_ids
    ):
        customer = await create_customer(SHOP_ID, "Test MultiSvc Customer")
        cleanup_customer_ids.append(customer["id"])

        start = _next_weekday_10am() + timedelta(hours=4)

        # Taglio uomo (30 min) + Piega (30 min) = 60 min total
        appt = await create_appointment(
            SHOP_ID, customer["id"], STAFF_MIRCO,
            [SVC_TAGLIO_UOMO, SVC_PIEGA], start,
        )
        assert appt is not None
        cleanup_appointment_ids.append(appt["id"])

        # Verify end_time is 60 min after start
        end = appt["end_time"]
        if isinstance(end, str):
            end = datetime.fromisoformat(end)
        start_parsed = appt["start_time"]
        if isinstance(start_parsed, str):
            start_parsed = datetime.fromisoformat(start_parsed)
        duration = end - start_parsed
        assert duration == timedelta(minutes=60)


class TestCancelAppointmentLive:
    async def test_cancels_scheduled_appointment(
        self, db_connection, cleanup_customer_ids, cleanup_appointment_ids
    ):
        customer = await create_customer(SHOP_ID, "Test Cancel Customer")
        cleanup_customer_ids.append(customer["id"])

        start = _next_weekday_10am() + timedelta(hours=6)
        appt = await create_appointment(
            SHOP_ID, customer["id"], STAFF_MIRCO,
            [SVC_TAGLIO_UOMO], start,
        )
        cleanup_appointment_ids.append(appt["id"])

        cancelled = await cancel_appointment(SHOP_ID, appt["id"])
        assert cancelled is not None
        assert cancelled["status"] == "cancelled"

    async def test_cannot_cancel_already_cancelled(
        self, db_connection, cleanup_customer_ids, cleanup_appointment_ids
    ):
        customer = await create_customer(SHOP_ID, "Test DoubleCancel Customer")
        cleanup_customer_ids.append(customer["id"])

        start = _next_weekday_10am() + timedelta(days=1)
        appt = await create_appointment(
            SHOP_ID, customer["id"], STAFF_MIRCO,
            [SVC_TAGLIO_UOMO], start,
        )
        cleanup_appointment_ids.append(appt["id"])

        # Cancel once
        await cancel_appointment(SHOP_ID, appt["id"])
        # Try to cancel again — should return None
        result = await cancel_appointment(SHOP_ID, appt["id"])
        assert result is None


class TestRescheduleAppointmentLive:
    async def test_reschedules_to_new_time(
        self, db_connection, cleanup_customer_ids, cleanup_appointment_ids
    ):
        customer = await create_customer(SHOP_ID, "Test Reschedule Customer")
        cleanup_customer_ids.append(customer["id"])

        start = _next_weekday_10am() + timedelta(days=2)
        appt = await create_appointment(
            SHOP_ID, customer["id"], STAFF_MIRCO,
            [SVC_TAGLIO_UOMO], start,
        )
        cleanup_appointment_ids.append(appt["id"])

        new_start = start + timedelta(hours=3)
        rescheduled = await reschedule_appointment(
            SHOP_ID, appt["id"], new_start,
        )
        assert rescheduled is not None
        assert rescheduled["status"] == "scheduled"
        # The rescheduled appointment has a new ID
        assert rescheduled["id"] != appt["id"]
        cleanup_appointment_ids.append(rescheduled["id"])


class TestListAppointmentsLive:
    async def test_lists_customer_appointments(
        self, db_connection, cleanup_customer_ids, cleanup_appointment_ids
    ):
        customer = await create_customer(SHOP_ID, "Test ListAppt Customer")
        cleanup_customer_ids.append(customer["id"])

        start = _next_weekday_10am() + timedelta(days=3)
        appt = await create_appointment(
            SHOP_ID, customer["id"], STAFF_MIRCO,
            [SVC_TAGLIO_UOMO], start,
        )
        cleanup_appointment_ids.append(appt["id"])

        appts = await list_appointments(SHOP_ID, customer["id"])
        assert len(appts) >= 1
        found = [a for a in appts if a["id"] == appt["id"]]
        assert len(found) == 1
        assert found[0]["staff_name"] == "Mirco Meazzo"
        assert len(found[0]["services"]) >= 1
