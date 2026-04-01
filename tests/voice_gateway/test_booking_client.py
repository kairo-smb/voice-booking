"""Unit tests for BookingClient with mocked HTTP."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest

from voice_gateway.clients.booking_client import BookingClient

SHOP = UUID("a0000000-0000-0000-0000-000000000001")
STAFF = UUID("b0000000-0000-0000-0000-000000000001")
SVC = UUID("c0000000-0000-0000-0000-000000000001")
CUST = UUID("d0000000-0000-0000-0000-000000000001")
APPT = UUID("e0000000-0000-0000-0000-000000000001")
BASE = "http://test-booking:8000"


def _resp(status: int, json_data=None) -> httpx.Response:
    """Build a real httpx.Response with JSON body."""
    import json
    content = json.dumps(json_data).encode() if json_data is not None else b""
    return httpx.Response(status, content=content, headers={"content-type": "application/json"})


@pytest.fixture
async def bc():
    client = BookingClient(base_url=BASE)
    async with client:
        yield client


class TestBookingClientInit:
    def test_normalizes_url_without_scheme(self):
        bc = BookingClient(base_url="example.com")
        assert bc._base == "https://example.com"

    def test_strips_trailing_slash(self):
        bc = BookingClient(base_url="http://example.com/")
        assert bc._base == "http://example.com"


class TestBookingClientContextManager:
    async def test_raises_without_context(self):
        bc = BookingClient()
        with pytest.raises(RuntimeError, match="context manager"):
            _ = bc.client


class TestGetShop:
    async def test_returns_shop_on_200(self, bc):
        shop_data = {"id": str(SHOP), "name": "Salone"}
        bc._client.get = AsyncMock(return_value=_resp(200, shop_data))
        result = await bc.get_shop(SHOP)
        assert result == shop_data

    async def test_returns_none_on_404(self, bc):
        bc._client.get = AsyncMock(return_value=_resp(404, {"error": "not_found"}))
        result = await bc.get_shop(SHOP)
        assert result is None


class TestFindCustomersByPhone:
    async def test_returns_customers(self, bc):
        customers = [{"id": str(CUST), "full_name": "Anna"}]
        bc._client.get = AsyncMock(return_value=_resp(200, customers))
        result = await bc.find_customers_by_phone(SHOP, "+39123")
        assert len(result) == 1

    async def test_returns_empty_on_error(self, bc):
        bc._client.get = AsyncMock(return_value=_resp(500, None))
        result = await bc.find_customers_by_phone(SHOP, "+39123")
        assert result == []


class TestCreateCustomer:
    async def test_creates_customer(self, bc):
        customer = {"id": str(CUST), "full_name": "Anna"}
        bc._client.post = AsyncMock(return_value=_resp(201, customer))
        result = await bc.create_customer(SHOP, "Anna", "+39123")
        assert result["full_name"] == "Anna"


class TestGetServices:
    async def test_returns_services(self, bc):
        services = [{"id": str(SVC), "service_name": "Taglio"}]
        bc._client.get = AsyncMock(return_value=_resp(200, services))
        result = await bc.get_services(SHOP)
        assert len(result) == 1


class TestGetStaff:
    async def test_returns_staff(self, bc):
        staff = [{"id": str(STAFF), "full_name": "Maria"}]
        bc._client.get = AsyncMock(return_value=_resp(200, staff))
        result = await bc.get_staff(SHOP)
        assert len(result) == 1


class TestCheckAvailability:
    async def test_returns_availability(self, bc):
        avail = {"slots": [{"staff_id": str(STAFF)}]}
        bc._client.get = AsyncMock(return_value=_resp(200, avail))
        result = await bc.check_availability(SHOP, [SVC], date(2026, 4, 1), date(2026, 4, 1))
        assert len(result["slots"]) == 1


class TestBookAppointment:
    async def test_books_appointment(self, bc):
        appt = {"id": str(APPT), "status": "scheduled"}
        bc._client.post = AsyncMock(return_value=_resp(201, appt))
        result = await bc.book_appointment(SHOP, CUST, [SVC], STAFF, datetime(2026, 4, 1, 10, 0))
        assert result["status"] == "scheduled"


class TestListAppointments:
    async def test_lists_appointments(self, bc):
        appts = [{"id": str(APPT), "status": "scheduled"}]
        bc._client.get = AsyncMock(return_value=_resp(200, appts))
        result = await bc.list_appointments(SHOP, CUST)
        assert len(result) == 1


class TestCancelAppointment:
    async def test_cancels_appointment(self, bc):
        cancelled = {"id": str(APPT), "status": "cancelled"}
        bc._client.patch = AsyncMock(return_value=_resp(200, cancelled))
        result = await bc.cancel_appointment(SHOP, APPT)
        assert result["status"] == "cancelled"


class TestRescheduleAppointment:
    async def test_reschedules_appointment(self, bc):
        rescheduled = {"id": "new-id", "status": "scheduled"}
        bc._client.patch = AsyncMock(return_value=_resp(200, rescheduled))
        result = await bc.reschedule_appointment(SHOP, APPT, datetime(2026, 4, 2, 14, 0))
        assert result["status"] == "scheduled"
