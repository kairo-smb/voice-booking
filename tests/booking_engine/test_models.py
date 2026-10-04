"""Unit tests for booking_engine Pydantic models."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from booking_engine.api.models import (
    CreateAppointmentRequest,
    CreateCustomerRequest,
    RescheduleRequest,
    ShopResponse,
    ServiceResponse,
    CustomerResponse,
    AppointmentResponse,
    AppointmentServiceDetail,
    ErrorResponse,
)


class TestCreateAppointmentRequest:
    def test_valid_request(self):
        req = CreateAppointmentRequest(
            customer_id=uuid4(),
            service_ids=[uuid4()],
            staff_id=uuid4(),
            start_time=datetime(2026, 4, 1, 10, 0),
        )
        assert len(req.service_ids) == 1
        assert req.notes is None

    def test_empty_service_ids_rejected(self):
        with pytest.raises(ValidationError, match="service_ids"):
            CreateAppointmentRequest(
                customer_id=uuid4(),
                service_ids=[],
                staff_id=uuid4(),
                start_time=datetime(2026, 4, 1, 10, 0),
            )

    def test_multiple_services_accepted(self):
        ids = [uuid4(), uuid4(), uuid4()]
        req = CreateAppointmentRequest(
            customer_id=uuid4(),
            service_ids=ids,
            staff_id=uuid4(),
            start_time=datetime(2026, 4, 1, 10, 0),
        )
        assert len(req.service_ids) == 3

    def test_notes_optional(self):
        req = CreateAppointmentRequest(
            customer_id=uuid4(),
            service_ids=[uuid4()],
            staff_id=uuid4(),
            start_time=datetime(2026, 4, 1, 10, 0),
            notes="Prima visita",
        )
        assert req.notes == "Prima visita"


class TestCreateCustomerRequest:
    def test_valid_with_phone(self):
        req = CreateCustomerRequest(full_name="Anna Verdi", phone_number="+39123456789")
        assert req.phone_number == "+39123456789"

    def test_phone_optional(self):
        req = CreateCustomerRequest(full_name="Anna Verdi")
        assert req.phone_number is None

    def test_missing_name_rejected(self):
        with pytest.raises(ValidationError, match="full_name"):
            CreateCustomerRequest()


class TestRescheduleRequest:
    def test_valid_with_staff(self):
        req = RescheduleRequest(
            new_start_time=datetime(2026, 4, 2, 14, 0),
            new_staff_id=uuid4(),
        )
        assert req.new_staff_id is not None

    def test_staff_optional(self):
        req = RescheduleRequest(new_start_time=datetime(2026, 4, 2, 14, 0))
        assert req.new_staff_id is None


class TestShopResponse:
    def test_parses_from_dict(self):
        shop = ShopResponse(
            id=uuid4(), name="Salone", is_active=True,
        )
        assert shop.phone_number is None
        assert shop.is_active is True


class TestServiceResponse:
    def test_decimal_price(self):
        svc = ServiceResponse(
            id=uuid4(),
            service_name="Taglio donna",
            duration_minutes=30,
            price_eur=Decimal("25.50"),
        )
        assert svc.price_eur == Decimal("25.50")


class TestAppointmentResponse:
    def test_services_default_empty(self):
        appt = AppointmentResponse(
            id=uuid4(),
            customer_id=uuid4(),
            staff_id=uuid4(),
            start_time=datetime(2026, 4, 1, 10, 0),
            end_time=datetime(2026, 4, 1, 10, 30),
            status="scheduled",
        )
        assert appt.services == []


class TestErrorResponse:
    def test_error_fields(self):
        err = ErrorResponse(error="not_found", message="Shop not found")
        assert err.error == "not_found"
