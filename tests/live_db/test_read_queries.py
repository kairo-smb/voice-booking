"""Live DB tests — read-only queries against seed data.

These tests verify query functions work correctly against the real Neon PostgreSQL
database with the actual seed data from 02_seed_data.sql.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from booking_engine.db.queries import (
    get_shop,
    list_staff,
    list_services,
    get_staff_services,
    find_customers_by_phone,
    find_customers_by_name_and_phone,
)
from tests.live_db.conftest import (
    SHOP_ID,
    SHOP_ID_2,
    STAFF_MIRCO,
    STAFF_GIULIA,
    SVC_TAGLIO_DONNA,
    SVC_COLORE,
    SVC_PIEGA,
    CUSTOMER_MARIA,
    PHONE_MARIA,
    PHONE_LUCA,
)


class TestGetShopLive:
    async def test_returns_salon_bella(self, db_connection):
        shop = await get_shop(SHOP_ID)
        assert shop is not None
        assert shop["name"] == "Salon Bella"
        assert shop["is_active"] is True
        assert "benvenuto" in shop["welcome_message"].lower()

    async def test_returns_studio_hair(self, db_connection):
        shop = await get_shop(SHOP_ID_2)
        assert shop is not None
        assert shop["name"] == "Studio Hair"

    async def test_nonexistent_shop_returns_none(self, db_connection):
        fake_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        shop = await get_shop(fake_id)
        assert shop is None


class TestListStaffLive:
    async def test_salon_bella_has_three_staff(self, db_connection):
        staff = await list_staff(SHOP_ID)
        assert len(staff) == 3
        names = {s["full_name"] for s in staff}
        assert "Mirco Meazzo" in names
        assert "Giulia Verdi" in names
        assert "Marco Bianchi" in names

    async def test_staff_sorted_by_name(self, db_connection):
        staff = await list_staff(SHOP_ID)
        names = [s["full_name"] for s in staff]
        assert names == sorted(names)

    async def test_studio_hair_has_two_staff(self, db_connection):
        staff = await list_staff(SHOP_ID_2)
        assert len(staff) == 2


class TestListServicesLive:
    async def test_salon_bella_has_six_services(self, db_connection):
        services = await list_services(SHOP_ID)
        assert len(services) == 6
        names = {s["service_name"] for s in services}
        assert "Taglio donna" in names
        assert "Taglio uomo" in names
        assert "Colore" in names
        assert "Piega" in names

    async def test_service_has_price_and_duration(self, db_connection):
        services = await list_services(SHOP_ID)
        taglio = next(s for s in services if s["service_name"] == "Taglio donna")
        assert taglio["duration_minutes"] == 45
        assert float(taglio["price_eur"]) == 35.00

    async def test_services_sorted_by_category_then_name(self, db_connection):
        services = await list_services(SHOP_ID)
        categories = [s["category"] for s in services]
        # Should be grouped by category (sorted)
        assert categories == sorted(categories)


class TestGetStaffServicesLive:
    async def test_mirco_does_taglio_and_piega(self, db_connection):
        services = await get_staff_services(STAFF_MIRCO)
        names = {s["service_name"] for s in services}
        assert "Taglio donna" in names
        assert "Taglio uomo" in names
        assert "Piega" in names
        assert "Colore" not in names  # Mirco doesn't do color

    async def test_giulia_does_colore_and_trattamento(self, db_connection):
        services = await get_staff_services(STAFF_GIULIA)
        names = {s["service_name"] for s in services}
        assert "Colore" in names
        assert "Meches" in names
        assert "Trattamento cheratina" in names
        assert "Taglio donna" not in names  # Giulia doesn't do cuts


class TestFindCustomersLive:
    async def test_find_maria_by_phone(self, db_connection):
        customers = await find_customers_by_phone(SHOP_ID, PHONE_MARIA)
        assert len(customers) >= 1
        assert any(c["full_name"] == "Maria Rossi" for c in customers)

    async def test_find_by_name_and_phone(self, db_connection):
        customers = await find_customers_by_name_and_phone(SHOP_ID, "Maria", PHONE_MARIA)
        assert len(customers) >= 1
        assert customers[0]["full_name"] == "Maria Rossi"

    async def test_wrong_phone_returns_empty(self, db_connection):
        customers = await find_customers_by_phone(SHOP_ID, "+39 000 0000000")
        assert customers == []

    async def test_wrong_name_returns_empty(self, db_connection):
        customers = await find_customers_by_name_and_phone(SHOP_ID, "Nonexistent", PHONE_MARIA)
        assert customers == []
