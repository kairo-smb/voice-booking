"""Unit tests for query functions (mocked DB)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from booking_engine.db.queries import (
    SlotConflictError,
    cancel_appointment,
    create_appointment,
    create_customer,
    find_customers_by_name_and_phone,
    find_customers_by_phone,
    get_shop,
    get_staff_services,
    list_appointments,
    list_services,
    list_staff,
    reschedule_appointment,
)

SHOP = UUID("a0000000-0000-0000-0000-000000000001")
STAFF = UUID("11111111-0000-0000-0000-000000000001")
SVC = UUID("aaaa0001-0000-0000-0000-000000000001")
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
