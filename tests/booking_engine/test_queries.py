"""Unit tests for query functions (mocked DB)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from booking_engine.db.queries import (
    SlotConflictError,
    cancel_appointment,
    create_appointment,
    create_appointment_chain,
    create_customer,
    find_customers_by_name_and_phone,
    find_customers_by_phone,
    get_available_slot_chains,
    get_shop,
    get_staff_services,
    list_appointments,
    list_services,
    list_staff,
    reschedule_appointment,
)

SHOP = UUID("a0000000-0000-0000-0000-000000000001")
STAFF = UUID("11111111-0000-0000-0000-000000000001")
STAFF2 = UUID("11111111-0000-0000-0000-000000000002")
SVC = UUID("aaaa0001-0000-0000-0000-000000000001")
SVC2 = UUID("aaaa0001-0000-0000-0000-000000000002")
CUSTOMER = UUID("cccc0001-0000-0000-0000-000000000001")
APPT = UUID("dddddddd-0000-0000-0000-000000000001")
_ROME = ZoneInfo("Europe/Rome")


class TestGetShop:
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_found(self, mock_exec):
        mock_exec.return_value = {"id": SHOP, "name": "Salon Bella", "is_active": True}
        result = await get_shop(SHOP)
        assert result["name"] == "Salon Bella"
        mock_exec.assert_called_once()
        # Verify positional args: SQL string, then UUID
        args = mock_exec.call_args.args
        assert SHOP in args

    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_not_found(self, mock_exec):
        mock_exec.return_value = None
        result = await get_shop(SHOP)
        assert result is None


class TestListStaff:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_returns_list(self, mock_exec):
        mock_exec.return_value = [{"id": STAFF, "full_name": "Mirco", "role": "stilista", "bio": "test"}]
        result = await list_staff(SHOP)
        assert len(result) == 1
        assert result[0]["full_name"] == "Mirco"


class TestListServices:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_returns_list(self, mock_exec):
        mock_exec.return_value = [{"id": SVC, "service_name": "Taglio donna", "duration_minutes": 45, "price_eur": Decimal("35.00"), "category": "taglio"}]
        result = await list_services(SHOP)
        assert len(result) == 1


class TestGetStaffServices:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_returns_list(self, mock_exec):
        mock_exec.return_value = [{"id": SVC, "service_name": "Taglio donna", "duration_minutes": 45, "price_eur": Decimal("35.00"), "category": "taglio"}]
        result = await get_staff_services(STAFF)
        assert len(result) == 1


class TestFindCustomers:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_by_phone_found(self, mock_exec):
        mock_exec.return_value = [{"id": CUSTOMER, "full_name": "Maria Rossi"}]
        result = await find_customers_by_phone(SHOP, "+39 333 1111111")
        assert len(result) == 1

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_by_phone_empty(self, mock_exec):
        mock_exec.return_value = []
        result = await find_customers_by_phone(SHOP, "+39 000 0000000")
        assert result == []

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_by_name_and_phone(self, mock_exec):
        mock_exec.return_value = [{"id": CUSTOMER, "full_name": "Maria Rossi"}]
        result = await find_customers_by_name_and_phone(SHOP, "Maria", "+39 333 1111111")
        assert len(result) == 1


class TestCreateCustomer:
    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_without_phone(self, mock_one, mock_void):
        mock_one.return_value = {"id": CUSTOMER, "full_name": "Test"}
        result = await create_customer(SHOP, "Test")
        assert result["full_name"] == "Test"
        mock_void.assert_called_once()  # INSERT customer
        mock_one.assert_called_once()   # SELECT back

    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_with_phone(self, mock_one, mock_void):
        mock_one.side_effect = [
            {"id": CUSTOMER, "full_name": "Test"},  # SELECT customer
            None,  # no existing phone_contact
        ]
        result = await create_customer(SHOP, "Test", "+39 333 9999999")
        assert result["full_name"] == "Test"
        assert mock_void.call_count == 2  # INSERT customer + INSERT phone_contact


class TestCreateAppointment:
    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_success(self, mock_exec, mock_one, mock_void):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 45, "price_eur": Decimal("35.00")}],  # services
            [],  # no overlap
        ]
        mock_one.return_value = {"id": APPT, "status": "scheduled"}
        start = datetime(2026, 5, 5, 10, 0, tzinfo=_ROME)
        result = await create_appointment(SHOP, CUSTOMER, STAFF, [SVC], start)
        assert result["status"] == "scheduled"

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_conflict(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 45, "price_eur": Decimal("35.00")}],
            [{"id": "existing"}],  # overlap found
        ]
        start = datetime(2026, 5, 5, 10, 0, tzinfo=_ROME)
        with pytest.raises(SlotConflictError):
            await create_appointment(SHOP, CUSTOMER, STAFF, [SVC], start)


class TestCancelAppointment:
    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_success(self, mock_one, mock_void):
        mock_one.side_effect = [
            {"id": APPT, "status": "scheduled"},
            {"id": APPT, "status": "cancelled"},
        ]
        result = await cancel_appointment(SHOP, APPT)
        assert result["status"] == "cancelled"

    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    async def test_not_cancellable(self, mock_one):
        mock_one.return_value = None
        result = await cancel_appointment(SHOP, APPT)
        assert result is None


class TestListAppointments:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_returns_with_services(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": APPT, "staff_name": "Mirco", "status": "scheduled"}],
            [{"service_id": SVC, "service_name": "Taglio", "duration_minutes": 45, "price_eur": Decimal("35.00")}],
        ]
        result = await list_appointments(SHOP)
        assert len(result) == 1
        assert "services" in result[0]


class TestGetAvailableSlotChains:
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_single_day_two_legs_different_staff(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30}, {"id": SVC2, "duration_minutes": 30}],  # durations
            [{"staff_id": STAFF, "staff_name": "Ana"}],  # eligible leg0
            [{"staff_id": STAFF2, "staff_name": "Bob"}],  # eligible leg1
            [],  # existing appointments
            [{"start_time": "09:00:00", "end_time": "17:00:00"}],  # staff0 day windows
            [{"start_time": "09:00:00", "end_time": "17:00:00"}],  # staff1 day windows
        ]
        day = date(2026, 5, 5)
        result = await get_available_slot_chains(
            SHOP, [{"service_id": SVC, "staff_id": None},
                   {"service_id": SVC2, "staff_id": None}],
            day, day, max_results=1,
        )
        assert len(result) == 1
        legs = result[0]["legs"]
        assert legs[0]["staff_id"] == STAFF and legs[1]["staff_id"] == STAFF2
        assert legs[0]["slot_end"] == legs[1]["slot_start"]  # back-to-back, 0-minute gap

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_duplicate_service_id_in_chain_is_not_misreported_as_unknown(self, mock_exec):
        # Same service booked twice (two different staff) in one chain: the
        # services list has 2 entries for SVC, but the DB returns only 1
        # distinct row for it. Must not be treated as "unknown service".
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30}],  # durations: 1 distinct row for a duplicated id
            [{"staff_id": STAFF, "staff_name": "Ana"}],  # eligible leg0 (staff_id=STAFF pinned)
            [{"staff_id": STAFF2, "staff_name": "Bob"}],  # eligible leg1 (staff_id=STAFF2 pinned)
            [],  # existing appointments
            [{"start_time": "09:00:00", "end_time": "17:00:00"}],  # staff0 day windows
            [{"start_time": "09:00:00", "end_time": "17:00:00"}],  # staff1 day windows
        ]
        day = date(2026, 5, 5)
        result = await get_available_slot_chains(
            SHOP, [{"service_id": SVC, "staff_id": STAFF},
                   {"service_id": SVC, "staff_id": STAFF2}],
            day, day, max_results=1,
        )
        assert len(result) == 1
        legs = result[0]["legs"]
        assert legs[0]["service_id"] == SVC and legs[1]["service_id"] == SVC

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_no_eligible_staff_for_second_leg_returns_empty(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30}, {"id": SVC2, "duration_minutes": 30}],
            [{"staff_id": STAFF, "staff_name": "Ana"}],
            [],  # no one eligible for leg1
        ]
        day = date(2026, 5, 5)
        result = await get_available_slot_chains(
            SHOP, [{"service_id": SVC, "staff_id": None},
                   {"service_id": SVC2, "staff_id": None}],
            day, day,
        )
        assert result == []

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_leg1_skips_conflicting_start_and_finds_later_one_within_gap(self, mock_exec):
        # STAFF2 has an existing 09:30-09:40 appointment. The naive
        # earliest-only candidate (09:30, right at prev_end) would conflict;
        # the search must step forward in 5-min increments and land on
        # 09:40 (still within MAX_GAP_MINUTES of prev_end=09:30).
        existing = [{
            "staff_id": STAFF2,
            "start_time": datetime(2026, 5, 5, 9, 30, tzinfo=_ROME),
            "end_time": datetime(2026, 5, 5, 9, 40, tzinfo=_ROME),
        }]
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30}, {"id": SVC2, "duration_minutes": 30}],  # durations
            [{"staff_id": STAFF, "staff_name": "Ana"}],  # eligible leg0
            [{"staff_id": STAFF2, "staff_name": "Bob"}],  # eligible leg1
            existing,  # existing appointments
            [{"start_time": "09:00:00", "end_time": "17:00:00"}],  # staff0 day windows
            [{"start_time": "09:00:00", "end_time": "17:00:00"}],  # staff1 day windows
        ]
        day = date(2026, 5, 5)
        result = await get_available_slot_chains(
            SHOP, [{"service_id": SVC, "staff_id": None},
                   {"service_id": SVC2, "staff_id": None}],
            day, day, max_results=1,
        )
        assert len(result) == 1
        legs = result[0]["legs"]
        assert legs[0]["slot_end"] == datetime(2026, 5, 5, 9, 30, tzinfo=_ROME)
        assert legs[1]["slot_start"] == datetime(2026, 5, 5, 9, 40, tzinfo=_ROME)
        assert legs[1]["slot_end"] == datetime(2026, 5, 5, 10, 10, tzinfo=_ROME)

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_unknown_service_in_chain_returns_empty(self, mock_exec):
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30}],  # only one of two services found/active
        ]
        day = date(2026, 5, 5)
        result = await get_available_slot_chains(
            SHOP, [{"service_id": SVC, "staff_id": None},
                   {"service_id": SVC2, "staff_id": None}],
            day, day,
        )
        assert result == []


class TestCreateAppointmentChain:
    @patch("booking_engine.db.queries.execute_void", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute_one", new_callable=AsyncMock)
    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_success(self, mock_exec, mock_one, mock_void):
        leg1_start = datetime(2026, 5, 5, 9, 0, tzinfo=_ROME)
        leg2_start = datetime(2026, 5, 5, 9, 30, tzinfo=_ROME)
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30, "price_eur": Decimal("35.00")},
             {"id": SVC2, "duration_minutes": 30, "price_eur": Decimal("20.00")}],  # durations
            [],  # leg1 overlap check
            [],  # leg2 overlap check
        ]
        mock_one.return_value = {"id": APPT, "status": "scheduled"}
        legs = [
            {"service_id": SVC, "staff_id": STAFF, "slot_start": leg1_start},
            {"service_id": SVC2, "staff_id": STAFF2, "slot_start": leg2_start},
        ]
        result = await create_appointment_chain(SHOP, CUSTOMER, legs)
        assert result["status"] == "scheduled"
        assert mock_void.await_count == 3  # 1 appointment insert + 2 appointment_services inserts

    @patch("booking_engine.db.queries.execute", new_callable=AsyncMock)
    async def test_conflict_on_second_leg(self, mock_exec):
        leg1_start = datetime(2026, 5, 5, 9, 0, tzinfo=_ROME)
        leg2_start = datetime(2026, 5, 5, 9, 30, tzinfo=_ROME)
        mock_exec.side_effect = [
            [{"id": SVC, "duration_minutes": 30, "price_eur": Decimal("35.00")},
             {"id": SVC2, "duration_minutes": 30, "price_eur": Decimal("20.00")}],
            [],  # leg1 overlap check: clear
            [{"id": "existing"}],  # leg2 overlap check: conflict
        ]
        legs = [
            {"service_id": SVC, "staff_id": STAFF, "slot_start": leg1_start},
            {"service_id": SVC2, "staff_id": STAFF2, "slot_start": leg2_start},
        ]
        with pytest.raises(SlotConflictError):
            await create_appointment_chain(SHOP, CUSTOMER, legs)
