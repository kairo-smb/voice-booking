"""Integration tests — complete booking workflows via the REST API.

These tests exercise multiple routes in sequence, mocking only the DB query
layer to simulate end-to-end API behaviour without a real database.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tests.integration.conftest import SHOP_ID, STAFF_ID, SVC_ID, FakeDB

ROME = ZoneInfo("Europe/Rome")


class TestCustomerCreationAndLookup:
    """Create a customer, then look them up by phone."""

    def test_create_then_lookup(self, client, fake_db):
        cust_id = str(uuid4())
        created = {"id": cust_id, "full_name": "Anna Verdi", "preferred_staff_id": None, "notes": None}

        with patch("booking_engine.api.routes.customers.create_customer",
                    new_callable=AsyncMock, return_value=created):
            resp = client.post(
                f"/api/v1/shops/{SHOP_ID}/customers",
                json={"full_name": "Anna Verdi", "phone_number": "+39123456789"},
            )
        assert resp.status_code == 201
        customer_id = resp.json()["id"]

        with patch("booking_engine.api.routes.customers.find_customers_by_phone",
                    new_callable=AsyncMock, return_value=[created]):
            resp = client.get(
                f"/api/v1/shops/{SHOP_ID}/customers",
                params={"phone": "+39123456789"},
            )
        assert resp.status_code == 200
        assert resp.json()[0]["id"] == customer_id


class TestFullBookingWorkflow:
    """Check services → check availability → book → list → cancel."""

    def test_complete_booking_lifecycle(self, client, fake_db):
        # 1. List services
        with patch("booking_engine.api.routes.services.list_services",
                    new_callable=AsyncMock, return_value=fake_db.services):
            resp = client.get(f"/api/v1/shops/{SHOP_ID}/services")
        assert resp.status_code == 200
        services = resp.json()
        assert len(services) == 1
        service_id = services[0]["id"]

        # 2. List staff
        with patch("booking_engine.api.routes.services.list_staff",
                    new_callable=AsyncMock, return_value=fake_db.staff):
            resp = client.get(f"/api/v1/shops/{SHOP_ID}/staff")
        assert resp.status_code == 200
        staff = resp.json()
        staff_id = staff[0]["id"]

        # 3. Check availability
        start = datetime(2026, 4, 1, 10, 0, tzinfo=ROME)
        slot = {
            "staff_id": staff_id, "staff_name": "Maria Rossi",
            "slot_start": start.isoformat(),
            "slot_end": (start + timedelta(minutes=30)).isoformat(),
        }
        with patch("booking_engine.api.routes.availability.get_available_slots",
                    new_callable=AsyncMock, return_value=[slot]):
            resp = client.get(
                f"/api/v1/shops/{SHOP_ID}/availability",
                params={
                    "service_ids": service_id,
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-01",
                },
            )
        assert resp.status_code == 200
        slots = resp.json()["slots"]
        assert len(slots) == 1

        # 4. Create customer
        cust_id = str(uuid4())
        cust = {"id": cust_id, "full_name": "Anna Verdi", "preferred_staff_id": None, "notes": None}
        with patch("booking_engine.api.routes.customers.create_customer",
                    new_callable=AsyncMock, return_value=cust):
            resp = client.post(
                f"/api/v1/shops/{SHOP_ID}/customers",
                json={"full_name": "Anna Verdi", "phone_number": "+39123"},
            )
        assert resp.status_code == 201
        customer_id = resp.json()["id"]

        # 5. Book appointment
        appt_id = str(uuid4())
        appt = {
            "id": appt_id, "customer_id": customer_id,
            "staff_id": staff_id, "staff_name": "Maria Rossi",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=30)).isoformat(),
            "status": "scheduled",
            "services": [{"service_id": service_id, "service_name": "Taglio donna",
                          "duration_minutes": 30, "price_eur": Decimal("25.00")}],
            "notes": None,
        }
        with patch("booking_engine.api.routes.appointments.create_appointment",
                    new_callable=AsyncMock, return_value=appt):
            resp = client.post(
                f"/api/v1/shops/{SHOP_ID}/appointments",
                json={
                    "customer_id": customer_id,
                    "service_ids": [service_id],
                    "staff_id": staff_id,
                    "start_time": start.isoformat(),
                },
            )
        assert resp.status_code == 201
        assert resp.json()["status"] == "scheduled"
        appointment_id = resp.json()["id"]

        # 6. List appointments
        with patch("booking_engine.api.routes.appointments.list_appointments",
                    new_callable=AsyncMock, return_value=[appt]):
            resp = client.get(
                f"/api/v1/shops/{SHOP_ID}/appointments",
                params={"customer_id": customer_id},
            )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # 7. Cancel appointment
        cancelled = {**appt, "status": "cancelled"}
        with patch("booking_engine.api.routes.appointments.cancel_appointment",
                    new_callable=AsyncMock, return_value=cancelled):
            resp = client.patch(f"/api/v1/shops/{SHOP_ID}/appointments/{appointment_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


class TestRescheduleWorkflow:
    """Book → reschedule to new time."""

    def test_book_then_reschedule(self, client, fake_db):
        start = datetime(2026, 4, 1, 10, 0, tzinfo=ROME)
        new_start = datetime(2026, 4, 2, 14, 0, tzinfo=ROME)
        cust_id = str(uuid4())
        appt_id = str(uuid4())

        appt = {
            "id": appt_id, "customer_id": cust_id,
            "staff_id": str(STAFF_ID), "staff_name": "Maria Rossi",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=30)).isoformat(),
            "status": "scheduled",
            "services": [{"service_id": str(SVC_ID), "service_name": "Taglio donna",
                          "duration_minutes": 30, "price_eur": Decimal("25.00")}],
            "notes": None,
        }

        # Book
        with patch("booking_engine.api.routes.appointments.create_appointment",
                    new_callable=AsyncMock, return_value=appt):
            resp = client.post(
                f"/api/v1/shops/{SHOP_ID}/appointments",
                json={
                    "customer_id": cust_id,
                    "service_ids": [str(SVC_ID)],
                    "staff_id": str(STAFF_ID),
                    "start_time": start.isoformat(),
                },
            )
        assert resp.status_code == 201

        # Reschedule
        new_appt_id = str(uuid4())
        rescheduled = {
            **appt,
            "id": new_appt_id,
            "start_time": new_start.isoformat(),
            "end_time": (new_start + timedelta(minutes=30)).isoformat(),
        }
        with patch("booking_engine.api.routes.appointments.reschedule_appointment",
                    new_callable=AsyncMock, return_value=rescheduled):
            resp = client.patch(
                f"/api/v1/shops/{SHOP_ID}/appointments/{appt_id}/reschedule",
                json={"new_start_time": new_start.isoformat()},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == new_appt_id


class TestSlotConflictHandling:
    """Attempt to double-book the same slot — expect 409."""

    def test_double_booking_returns_409(self, client, fake_db):
        from booking_engine.db.queries import SlotConflictError

        start = datetime(2026, 4, 1, 10, 0, tzinfo=ROME)
        cust_id = str(uuid4())
        appt = {
            "id": str(uuid4()), "customer_id": cust_id,
            "staff_id": str(STAFF_ID), "staff_name": "Maria Rossi",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=30)).isoformat(),
            "status": "scheduled", "services": [], "notes": None,
        }

        # First booking succeeds
        with patch("booking_engine.api.routes.appointments.create_appointment",
                    new_callable=AsyncMock, return_value=appt):
            resp = client.post(
                f"/api/v1/shops/{SHOP_ID}/appointments",
                json={
                    "customer_id": cust_id,
                    "service_ids": [str(SVC_ID)],
                    "staff_id": str(STAFF_ID),
                    "start_time": start.isoformat(),
                },
            )
        assert resp.status_code == 201

        # Second booking conflicts
        with patch("booking_engine.api.routes.appointments.create_appointment",
                    new_callable=AsyncMock, side_effect=SlotConflictError("overlap")):
            resp = client.post(
                f"/api/v1/shops/{SHOP_ID}/appointments",
                json={
                    "customer_id": cust_id,
                    "service_ids": [str(SVC_ID)],
                    "staff_id": str(STAFF_ID),
                    "start_time": start.isoformat(),
                },
            )
        assert resp.status_code == 409
        assert resp.json()["error"] == "slot_taken"


class TestHealthEndpoint:
    """Verify the health endpoint works in isolation."""

    def test_health_returns_ok(self):
        from booking_engine.api.app import create_app
        from unittest.mock import patch, AsyncMock

        with (
            patch("booking_engine.api.app.Settings") as MockSettings,
            patch("booking_engine.api.app.init_connection", new_callable=AsyncMock),
            patch("booking_engine.api.app.close_connection", new_callable=AsyncMock),
        ):
            mock_settings = MockSettings.return_value
            mock_settings.database_url = "postgresql://fake"
            mock_settings.pool_min_size = 2
            mock_settings.pool_max_size = 10

            app = create_app()
            with TestClient(app) as tc:
                resp = tc.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
