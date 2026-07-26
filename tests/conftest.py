"""Shared test fixtures — UUIDs, fake data factories."""
from __future__ import annotations

from datetime import datetime, date, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

ROME = ZoneInfo("Europe/Rome")

# ── Fixed UUIDs for deterministic tests ───────────────────────
SHOP_ID = UUID("a0000000-0000-0000-0000-000000000001")
STAFF_ID_1 = UUID("b0000000-0000-0000-0000-000000000001")
STAFF_ID_2 = UUID("b0000000-0000-0000-0000-000000000002")
SERVICE_ID_1 = UUID("c0000000-0000-0000-0000-000000000001")
SERVICE_ID_2 = UUID("c0000000-0000-0000-0000-000000000002")
CUSTOMER_ID = UUID("d0000000-0000-0000-0000-000000000001")
APPOINTMENT_ID = UUID("e0000000-0000-0000-0000-000000000001")


@pytest.fixture
def fake_shop() -> dict:
    return {
        "id": str(SHOP_ID),
        "name": "Salone Bella",
        "phone_number": "+39 012 345 6789",
        "address": "Via Roma 1, Milano",
        "is_active": True,
    }


@pytest.fixture
def fake_staff_list() -> list[dict]:
    return [
        {"id": str(STAFF_ID_1), "full_name": "Maria Rossi", "role": "stylist", "bio": "Senior stylist"},
        {"id": str(STAFF_ID_2), "full_name": "Luca Bianchi", "role": "barber", "bio": "Expert barber"},
    ]


@pytest.fixture
def fake_services_list() -> list[dict]:
    return [
        {
            "id": str(SERVICE_ID_1),
            "service_name": "Taglio donna",
            "description": "Taglio capelli donna",
            "duration_minutes": 30,
            "price_eur": Decimal("25.00"),
            "category": "Taglio",
        },
        {
            "id": str(SERVICE_ID_2),
            "service_name": "Piega",
            "description": "Piega capelli",
            "duration_minutes": 20,
            "price_eur": Decimal("15.00"),
            "category": "Styling",
        },
    ]


@pytest.fixture
def fake_customer() -> dict:
    return {
        "id": str(CUSTOMER_ID),
        "full_name": "Anna Verdi",
        "preferred_staff_id": None,
        "notes": None,
    }


@pytest.fixture
def fake_appointment() -> dict:
    start = datetime(2026, 4, 1, 10, 0, tzinfo=ROME)
    end = start + timedelta(minutes=30)
    return {
        "id": str(APPOINTMENT_ID),
        "shop_id": str(SHOP_ID),
        "customer_id": str(CUSTOMER_ID),
        "staff_id": str(STAFF_ID_1),
        "staff_name": "Maria Rossi",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "status": "scheduled",
        "services": [
            {
                "service_id": str(SERVICE_ID_1),
                "service_name": "Taglio donna",
                "duration_minutes": 30,
                "price_eur": Decimal("25.00"),
            }
        ],
        "notes": None,
    }
