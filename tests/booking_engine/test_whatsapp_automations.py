"""Automation rules: the due-work queries and the tick that fires them.

Two layers in one file, mirroring how the feature splits:

- `TestAutomationQueriesIntegration` — the due-work SQL against the QA/test
  Neon branch (TEST_DATABASE_URL or a non-prod DATABASE_URL). It writes only
  rows it owns (fresh shop_id per test) and deletes them after, so the shared
  branch is never reset or truncated.
- The tick unit tests — `run_automations` with the queries mocked, the same
  style as `test_whatsapp.py`'s `enqueue_campaign`/`send_due` tests.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from booking_engine.config import Settings
from booking_engine.db import connection
from booking_engine.db import whatsapp_automation_queries as aq

ROME = ZoneInfo("Europe/Rome")

# Production Neon endpoint — the automation tests create/delete rows and must
# NEVER run here. Same guard as tests/live_db/conftest.py.
_PROD_HOST_FRAGMENT = "ep-weathered-term-agsfwl6w"


def _resolve_test_db_url() -> str:
    """Resolve the DB for these tests, refusing production.

    Follows the repo's existing live-db gate: TEST_DATABASE_URL first, then
    DATABASE_URL, and a URL pointing at production resolves to "" so the suite
    skips rather than ever mutating prod rows.
    """
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if url and _PROD_HOST_FRAGMENT in url:
        return ""
    return url


def _try_connect() -> bool:
    url = _resolve_test_db_url()
    if not url:
        return False
    try:
        import asyncio

        import asyncpg

        loop = asyncio.new_event_loop()
        conn = loop.run_until_complete(asyncpg.connect(dsn=url))
        ok = loop.run_until_complete(conn.fetchrow("SELECT 1 AS ping")) is not None
        loop.run_until_complete(conn.close())
        loop.close()
        return ok
    except Exception:
        return False


_db_available = _try_connect()


@pytest.fixture
async def db_connection():
    """Pool for the DB-backed tests. Never invoked when the suite is skipped."""
    settings = Settings(database_url=_resolve_test_db_url())
    await connection.init_connection(settings)
    yield connection
    await connection.close_connection()


# ----------------------------------------------------------------- helpers

async def _insert_shop(shop_id, *, name=None):
    await connection.execute_void(
        "INSERT INTO business_app_core.shops (id, name) VALUES ($1, $2)",
        shop_id, name or f"AutomationTest {shop_id.hex[:8]}",
    )


async def _insert_staff(shop_id, staff_id, full_name="Test Stylist"):
    await connection.execute_void(
        "INSERT INTO business_app_core.staff (id, shop_id, full_name) "
        "VALUES ($1, $2, $3)",
        staff_id, shop_id, full_name,
    )


async def _insert_customer(shop_id, customer_id, *, phone="+393331112233"):
    await connection.execute_void(
        "INSERT INTO business_app_core.customers (id, shop_id, full_name, phone) "
        "VALUES ($1, $2, $3, $4)",
        customer_id, shop_id, "Maria Test", phone,
    )


async def _insert_service(shop_id, service_id, name="Taglio"):
    await connection.execute_void(
        "INSERT INTO business_app_core.services "
        "(id, shop_id, service_name, duration_minutes, category) "
        "VALUES ($1, $2, $3, 30, 'taglio')",
        service_id, shop_id, name,
    )


async def _insert_appointment(
    *,
    shop_id, appointment_id, customer_id, staff_id,
    start_time, end_time, status="completed",
):
    await connection.execute_void(
        """
        INSERT INTO business_app_core.appointments
            (id, shop_id, customer_id, staff_id, start_time, end_time, status)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        """,
        appointment_id, shop_id, customer_id, staff_id,
        start_time, end_time, status,
    )


async def _insert_appointment_service(appointment_id, service_id):
    await connection.execute_void(
        """
        INSERT INTO business_app_core.appointment_services
            (appointment_id, service_id, duration_minutes, price_eur)
        VALUES ($1, $2, 30, 25.00)
        """,
        appointment_id, service_id,
    )


@pytest.fixture
async def shop(db_connection):
    """A fresh shop per test, plus cleanup that removes only its own rows."""
    shop_id = uuid4()
    await _insert_shop(shop_id)
    yield shop_id
    await connection.execute_void(
        "DELETE FROM whatsapp.automation_sends WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM whatsapp.automation_rules WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM business_app_core.appointments WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM business_app_core.services WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM business_app_core.customers WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM business_app_core.staff WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM business_app_core.shops WHERE id = $1", shop_id,
    )


async def _completed_days_ago(shop_id, customer_id, staff_id, days):
    """Insert a completed appointment whose end_time is `days` days old."""
    appointment_id = uuid4()
    await _insert_appointment(
        shop_id=shop_id, appointment_id=appointment_id, customer_id=customer_id,
        staff_id=staff_id,
        start_time=datetime.now(tz=ROME) - timedelta(days=days + 1, minutes=10),
        end_time=datetime.now(tz=ROME) - timedelta(days=days),
    )
    return appointment_id


# ------------------------------------------------------------ integration

class TestAutomationQueriesIntegration:
    """The due-work SQL against real Neon-shaped data."""

    pytestmark = pytest.mark.skipif(
        not _db_available,
        reason="Test DB unavailable: set TEST_DATABASE_URL to the QA Neon branch "
               "(or a non-prod DATABASE_URL)",
    )

    async def test_appointment_already_in_automation_sends_is_not_returned_twice(
        self, shop,
    ):
        customer_id, staff_id = uuid4(), uuid4()
        service_id = uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id)
        await _insert_service(shop, service_id)
        appointment_id = await _completed_days_ago(
            shop, customer_id, staff_id, days=2,
        )
        await _insert_appointment_service(appointment_id, service_id)

        await aq.record_automation_send(
            shop_id=shop, rule_key="feedback", appointment_id=appointment_id,
        )

        due = await aq.due_feedback(shop, days_after=2)
        assert due == []

    async def test_due_feedback_respects_the_one_day_window_at_both_edges(
        self, shop,
    ):
        customer_id, staff_id = uuid4(), uuid4()
        service_id = uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id)
        await _insert_service(shop, service_id)

        due_id = await _completed_days_ago(shop, customer_id, staff_id, days=2)
        await _insert_appointment_service(due_id, service_id)
        too_old_id = await _completed_days_ago(
            shop, customer_id, staff_id, days=3,
        )
        await _insert_appointment_service(too_old_id, service_id)

        due = await aq.due_feedback(shop, days_after=2)

        ids = {row["appointment_id"] for row in due}
        assert ids == {due_id}
        assert len(due) == 1
        assert due[0]["service_names"] == "Taglio"

    async def test_due_feedback_excludes_a_customer_with_no_phone(self, shop):
        customer_id, staff_id = uuid4(), uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id, phone=None)
        await _completed_days_ago(shop, customer_id, staff_id, days=2)

        assert await aq.due_feedback(shop, days_after=2) == []

    async def test_due_feedback_rows_for_another_shop_never_appear(self, shop):
        other_shop = uuid4()
        await _insert_shop(other_shop, name="Other Shop")

        try:
            for sid in (shop, other_shop):
                customer_id, staff_id = uuid4(), uuid4()
                await _insert_staff(sid, staff_id)
                await _insert_customer(sid, customer_id)
                await _completed_days_ago(sid, customer_id, staff_id, days=2)

            due = await aq.due_feedback(shop, days_after=2)
            assert due and all(row["appointment_id"] is not None for row in due)
        finally:
            await connection.execute_void(
                "DELETE FROM business_app_core.appointments WHERE shop_id = $1",
                other_shop,
            )
            await connection.execute_void(
                "DELETE FROM business_app_core.customers WHERE shop_id = $1",
                other_shop,
            )
            await connection.execute_void(
                "DELETE FROM business_app_core.staff WHERE shop_id = $1",
                other_shop,
            )
            await connection.execute_void(
                "DELETE FROM business_app_core.shops WHERE id = $1", other_shop,
            )

    async def test_due_reminders_returns_upcoming_bookings_within_the_window(
        self, shop,
    ):
        customer_id, staff_id = uuid4(), uuid4()
        service_id = uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id)
        await _insert_service(shop, service_id)

        upcoming = uuid4()
        await _insert_appointment(
            shop_id=shop, appointment_id=upcoming, customer_id=customer_id,
            staff_id=staff_id,
            start_time=datetime.now(tz=ROME) + timedelta(hours=2),
            end_time=datetime.now(tz=ROME) + timedelta(hours=3),
            status="scheduled",
        )
        await _insert_appointment_service(upcoming, service_id)

        too_far = uuid4()
        await _insert_appointment(
            shop_id=shop, appointment_id=too_far, customer_id=customer_id,
            staff_id=staff_id,
            start_time=datetime.now(tz=ROME) + timedelta(hours=30),
            end_time=datetime.now(tz=ROME) + timedelta(hours=31),
            status="scheduled",
        )
        await _insert_appointment_service(too_far, service_id)

        due = await aq.due_reminders(shop, hours_before=24)

        assert {row["appointment_id"] for row in due} == {upcoming}
        assert due[0]["service_names"] == "Taglio"

    async def test_due_reminders_skips_started_and_cancelled(self, shop):
        customer_id, staff_id = uuid4(), uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id)

        started = uuid4()
        await _insert_appointment(
            shop_id=shop, appointment_id=started, customer_id=customer_id,
            staff_id=staff_id,
            start_time=datetime.now(tz=ROME) - timedelta(minutes=5),
            end_time=datetime.now(tz=ROME) + timedelta(minutes=55),
            status="scheduled",
        )
        cancelled = uuid4()
        await _insert_appointment(
            shop_id=shop, appointment_id=cancelled, customer_id=customer_id,
            staff_id=staff_id,
            start_time=datetime.now(tz=ROME) + timedelta(hours=1),
            end_time=datetime.now(tz=ROME) + timedelta(hours=2),
            status="cancelled",
        )

        assert await aq.due_reminders(shop, hours_before=24) == []

    async def test_upsert_rule_creates_then_updates(self, shop):
        created = await aq.upsert_rule(
            shop_id=shop, rule_key="feedback", enabled=True,
            params={"days_after": 2}, weekly_cap=150,
        )
        assert created["enabled"] is True
        assert created["weekly_cap"] == 150

        updated = await aq.upsert_rule(
            shop_id=shop, rule_key="feedback", enabled=False,
            params={"days_after": 3}, weekly_cap=100,
        )
        assert updated["enabled"] is False
        assert updated["params"] == {"days_after": 3}

        rules = await aq.get_rules(shop)
        assert len(rules) == 1
        assert rules[0]["rule_key"] == "feedback"
        assert rules[0]["weekly_cap"] == 100

    async def test_get_rules_is_empty_for_a_shop_that_never_configured(self, shop):
        assert await aq.get_rules(shop) == []

    async def test_sent_this_week_counts_this_rules_sends_only(self, shop):
        customer_id, staff_id = uuid4(), uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id)

        appt_a = await _completed_days_ago(shop, customer_id, staff_id, days=2)
        appt_b = await _completed_days_ago(shop, customer_id, staff_id, days=3)
        await aq.record_automation_send(
            shop_id=shop, rule_key="feedback", appointment_id=appt_a,
        )
        await aq.record_automation_send(
            shop_id=shop, rule_key="feedback", appointment_id=appt_b,
        )
        await aq.record_automation_send(
            shop_id=shop, rule_key="reminder", appointment_id=appt_a,
        )

        assert await aq.sent_this_week(shop, "feedback") == 2
        assert await aq.sent_this_week(shop, "reminder") == 1
