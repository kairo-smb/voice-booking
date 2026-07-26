"""Integration test fixtures — full FastAPI app with mocked DB layer."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from booking_engine.api.routes import shops, customers, services, availability, appointments

ROME = ZoneInfo("Europe/Rome")
SHOP_ID = UUID("a0000000-0000-0000-0000-000000000001")
STAFF_ID = UUID("b0000000-0000-0000-0000-000000000001")
SVC_ID = UUID("c0000000-0000-0000-0000-000000000001")


class FakeDB:
    """In-memory fake database for integration tests."""

    def __init__(self):
        self.shop = {
            "id": str(SHOP_ID), "name": "Salone Test", "phone_number": "+39000",
            "address": "Via Test", "is_active": True,
        }
        self.staff = [
            {"id": str(STAFF_ID), "full_name": "Maria Rossi", "role": "stylist", "bio": ""},
        ]
        self.services = [
            {"id": str(SVC_ID), "service_name": "Taglio donna", "description": "",
             "duration_minutes": 30, "price_eur": Decimal("25.00"), "category": "Taglio"},
        ]
        self.customers: list[dict] = []
        self.appointments: list[dict] = []
        self.appointment_services: list[dict] = []

    def reset(self):
        self.customers.clear()
        self.appointments.clear()
        self.appointment_services.clear()


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def app(fake_db) -> FastAPI:
    app = FastAPI()
    app.include_router(shops.router, prefix="/api/v1")
    app.include_router(customers.router, prefix="/api/v1")
    app.include_router(services.router, prefix="/api/v1")
    app.include_router(availability.router, prefix="/api/v1")
    app.include_router(appointments.router, prefix="/api/v1")
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)
