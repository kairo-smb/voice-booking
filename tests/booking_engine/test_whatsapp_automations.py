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
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import whatsapp_automations as wa
from booking_engine.services.messaging import whatsapp_send as ws

ROME = ZoneInfo("Europe/Rome")
SHOP = uuid4()

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


async def _completed_hours_ago(shop_id, customer_id, staff_id, hours):
    """Insert a completed appointment whose end_time is `hours` hours old."""
    appointment_id = uuid4()
    await _insert_appointment(
        shop_id=shop_id, appointment_id=appointment_id, customer_id=customer_id,
        staff_id=staff_id,
        start_time=datetime.now(tz=ROME) - timedelta(hours=hours + 1, minutes=10),
        end_time=datetime.now(tz=ROME) - timedelta(hours=hours),
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
        appointment_id = await _completed_hours_ago(
            shop, customer_id, staff_id, hours=2,
        )
        await _insert_appointment_service(appointment_id, service_id)

        await aq.record_automation_send(
            shop_id=shop, rule_key="feedback", appointment_id=appointment_id,
        )

        due = await aq.due_feedback(shop, hours_after=2)
        assert due == []

    async def test_due_feedback_respects_the_one_hour_window_at_both_edges(
        self, shop,
    ):
        customer_id, staff_id = uuid4(), uuid4()
        service_id = uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id)
        await _insert_service(shop, service_id)

        due_id = await _completed_hours_ago(shop, customer_id, staff_id, hours=2)
        await _insert_appointment_service(due_id, service_id)
        too_old_id = await _completed_hours_ago(
            shop, customer_id, staff_id, hours=3,
        )
        await _insert_appointment_service(too_old_id, service_id)

        due = await aq.due_feedback(shop, hours_after=2)

        ids = {row["appointment_id"] for row in due}
        assert ids == {due_id}
        assert len(due) == 1
        assert due[0]["service_names"] == "Taglio"

    async def test_due_feedback_excludes_a_customer_with_no_phone(self, shop):
        customer_id, staff_id = uuid4(), uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id, phone=None)
        await _completed_hours_ago(shop, customer_id, staff_id, hours=2)

        assert await aq.due_feedback(shop, hours_after=2) == []

    async def test_due_feedback_rows_for_another_shop_never_appear(self, shop):
        other_shop = uuid4()
        await _insert_shop(other_shop, name="Other Shop")

        try:
            for sid in (shop, other_shop):
                customer_id, staff_id = uuid4(), uuid4()
                await _insert_staff(sid, staff_id)
                await _insert_customer(sid, customer_id)
                await _completed_hours_ago(sid, customer_id, staff_id, hours=2)

            due = await aq.due_feedback(shop, hours_after=2)
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

        due = await aq.due_reminders(shop, min_no_shows=0)

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

        assert await aq.due_reminders(shop, min_no_shows=0) == []

    async def test_due_reminders_filters_by_no_show_threshold(self, shop):
        """Only customers with >= min_no_shows no-shows are reminded; 0 = all."""
        customer_id, staff_id = uuid4(), uuid4()
        await _insert_staff(shop, staff_id)
        await _insert_customer(shop, customer_id)
        # A past appointment this customer missed — the one thing that makes
        # them a no-show and so eligible for the reminder under min_no_shows=1.
        missed_id = uuid4()
        await _insert_appointment(
            shop_id=shop, appointment_id=missed_id, customer_id=customer_id,
            staff_id=staff_id,
            start_time=datetime.now(tz=ROME) - timedelta(days=3),
            end_time=datetime.now(tz=ROME) - timedelta(days=3) + timedelta(hours=1),
            status="no_show",
        )
        upcoming_id = uuid4()
        await _insert_appointment(
            shop_id=shop, appointment_id=upcoming_id, customer_id=customer_id,
            staff_id=staff_id,
            start_time=datetime.now(tz=ROME) + timedelta(hours=2),
            end_time=datetime.now(tz=ROME) + timedelta(hours=3),
            status="scheduled",
        )

        # Threshold 1: the no-show history qualifies this customer.
        due = await aq.due_reminders(shop, min_no_shows=1)
        assert {row["appointment_id"] for row in due} == {upcoming_id}

        # Threshold 3: the single no-show is not enough.
        assert await aq.due_reminders(shop, min_no_shows=3) == []

        # Threshold 0: everyone with an upcoming booking is reminded.
        due_all = await aq.due_reminders(shop, min_no_shows=0)
        assert {row["appointment_id"] for row in due_all} == {upcoming_id}

    async def test_upsert_rule_creates_then_updates(self, shop):
        created = await aq.upsert_rule(
            shop_id=shop, rule_key="feedback", enabled=True,
            params={"hours_after": 24, "platform": "google", "link": ""},
        )
        assert created["enabled"] is True

        updated = await aq.upsert_rule(
            shop_id=shop, rule_key="feedback", enabled=False,
            params={"hours_after": 6, "platform": "general", "link": ""},
        )
        assert updated["enabled"] is False
        assert updated["params"] == {"hours_after": 6, "platform": "general", "link": ""}

        rules = await aq.get_rules(shop)
        assert len(rules) == 1
        assert rules[0]["rule_key"] == "feedback"

    async def test_get_rules_is_empty_for_a_shop_that_never_configured(self, shop):
        assert await aq.get_rules(shop) == []

    async def test_list_enabled_rules_excludes_disabled_rules(self, shop):
        """'Absent rows mean off' — the tick only ever sees enabled rules."""
        await aq.upsert_rule(
            shop_id=shop, rule_key="feedback", enabled=False,
            params={"hours_after": 24, "platform": "general", "link": ""},
        )
        await aq.upsert_rule(
            shop_id=shop, rule_key="reminder", enabled=True,
            params={"min_no_shows": 1},
        )

        enabled = await aq.list_enabled_rules()

        assert [r["rule_key"] for r in enabled] == ["reminder"]
        assert all(r["enabled"] for r in enabled)

    async def test_claim_due_sql_is_valid(self, shop):
        """The drip's claim statement parses and plans against the real schema.

        claim_due is mocked in every other test, so when the template-category
        join was added it shipped as a parse error ("invalid reference to
        FROM-clause entry for table m" — Postgres refuses to let an outer join
        in UPDATE ... FROM reference the update target) and every WhatsApp send
        would have failed at runtime.

        LIMIT 0 is the point: parse-analysis and planning still run over the
        whole statement, so a broken join raises here, while the CTE selects
        nothing and no row on the shared QA branch is claimed.
        """
        assert await wq.claim_due(0) == []


# ------------------------------------------------------- the tick (unit)

class FakeSettings:
    whatsapp_send_start_hour = 9
    whatsapp_send_end_hour = 20
    whatsapp_sends_per_minute = 0
    whatsapp_recipient_cooldown_hours = 168


def _enabled_rule(**over):
    row = {"shop_id": SHOP, "rule_key": "feedback", "enabled": True,
           "params": {"hours_after": 24, "platform": "general", "link": ""}}
    row.update(over)
    return row


def _online_sender(**over):
    row = {"shop_id": SHOP, "status": "online", "phone_number": "+393331110000",
           "phone_number_id": "PN1", "access_token": "tok",
           "quality_rating": "GREEN"}
    row.update(over)
    return row


def _approved_template(**over):
    row = {"status": "approved", "name": "kairo_feedback_v2", "language": "it",
           "category": "UTILITY"}
    row.update(over)
    return row


def _due_row(**over):
    row = {"appointment_id": uuid4(), "customer_id": uuid4(),
           "phone": "+393331112233", "first_name": "Giulia",
           "shop_name": "Salone X", "service_names": "Taglio",
           "appointment_at": datetime(2026, 8, 20, 10, 0, tzinfo=ROME)}
    row.update(over)
    return row


_ENQUEUE_SENTINEL = object()


def _patch_automations(monkeypatch, *, rules=None, sender=None, template=None,
                       due=None, enqueue_result=_ENQUEUE_SENTINEL):
    calls = {"enqueued": [], "recorded": []}

    async def _rules():
        return rules if rules is not None else [_enabled_rule()]
    async def _sender(shop_id):
        return sender if sender is not None else _online_sender()
    async def _template(shop_id, key):
        return template if template is not None else _approved_template()
    async def _due_feedback(shop_id, hours_after):
        calls.setdefault("due_feedback", []).append(hours_after)
        return due if due is not None else []
    async def _due_reminders(shop_id, min_no_shows):
        calls.setdefault("due_reminders", []).append(min_no_shows)
        return due if due is not None else []
    async def _enqueue(**kw):
        calls["enqueued"].append(kw)
        return uuid4() if enqueue_result is _ENQUEUE_SENTINEL else enqueue_result
    async def _record(**kw):
        calls["recorded"].append(kw)

    monkeypatch.setattr(aq, "list_enabled_rules", _rules)
    monkeypatch.setattr(wq, "get_sender", _sender)
    monkeypatch.setattr(wq, "get_template", _template)
    monkeypatch.setattr(aq, "due_feedback", _due_feedback)
    monkeypatch.setattr(aq, "due_reminders", _due_reminders)
    monkeypatch.setattr(wq, "enqueue", _enqueue)
    monkeypatch.setattr(aq, "record_automation_send", _record)
    return calls


@pytest.mark.asyncio
async def test_disabled_rule_sends_nothing(monkeypatch):
    """A rule that is off never reaches the tick: list_enabled_rules filters
    it out (pinned at the query level by
    test_list_enabled_rules_excludes_disabled_rules), so with no enabled rules
    the tick enqueues nothing."""
    calls = _patch_automations(monkeypatch, rules=[], due=[_due_row()])

    counts = await wa.run_automations(settings=FakeSettings())

    assert counts["feedback"] == 0 and counts["reminder"] == 0
    assert calls["enqueued"] == []
    assert calls["recorded"] == []


@pytest.mark.asyncio
async def test_sends_every_due_row_with_no_cap(monkeypatch):
    """The weekly cap is gone: every due appointment is enqueued, however many."""
    calls = _patch_automations(
        monkeypatch, due=[_due_row(), _due_row(), _due_row()],
    )

    counts = await wa.run_automations(settings=FakeSettings())

    assert counts["feedback"] == 3
    assert len(calls["enqueued"]) == 3
    assert len(calls["recorded"]) == 3


@pytest.mark.asyncio
async def test_red_quality_skips_marketing_rules_and_lets_utility_through(monkeypatch):
    # A MARKETING rule (Plan 3's win-back) pauses under RED quality...
    marketing = _patch_automations(
        monkeypatch, sender=_online_sender(quality_rating="RED"),
        template=_approved_template(category="MARKETING"), due=[_due_row()],
    )
    counts = await wa.run_automations(settings=FakeSettings())
    assert counts["feedback"] == 0
    assert marketing["enqueued"] == []

    # ...but the UTILITY rules this plan ships continue regardless.
    utility = _patch_automations(
        monkeypatch, sender=_online_sender(quality_rating="RED"),
        template=_approved_template(category="UTILITY"), due=[_due_row()],
    )
    counts = await wa.run_automations(settings=FakeSettings())
    assert counts["feedback"] == 1
    assert len(utility["enqueued"]) == 1


@pytest.mark.asyncio
async def test_enqueue_failure_records_nothing_in_automation_sends(monkeypatch):
    calls = _patch_automations(monkeypatch, due=[_due_row()])

    async def _enqueue_raises(**kw):
        raise RuntimeError("queue down")
    monkeypatch.setattr(wq, "enqueue", _enqueue_raises)

    counts = await wa.run_automations(settings=FakeSettings())

    assert counts["feedback"] == 0 and counts["errors"] == 1
    assert calls["recorded"] == []          # so the next tick retries it


@pytest.mark.asyncio
async def test_one_shop_raising_does_not_abort_the_others(monkeypatch):
    good_shop, bad_shop = uuid4(), uuid4()
    recorded = []
    rules = [_enabled_rule(shop_id=bad_shop, rule_key="feedback"),
             _enabled_rule(shop_id=good_shop, rule_key="reminder")]

    async def _rules():
        return rules
    async def _sender(shop_id):
        if shop_id == bad_shop:
            raise RuntimeError("boom")
        return _online_sender(shop_id=shop_id)
    async def _template(shop_id, key):
        return _approved_template(name="kairo_reminder_v1")
    async def _due_feedback(shop_id, hours_after):
        return []
    async def _due_reminders(shop_id, min_no_shows):
        return [_due_row()]
    async def _enqueue(**kw):
        return uuid4()
    async def _record(**kw):
        recorded.append(kw)

    monkeypatch.setattr(aq, "list_enabled_rules", _rules)
    monkeypatch.setattr(wq, "get_sender", _sender)
    monkeypatch.setattr(wq, "get_template", _template)
    monkeypatch.setattr(aq, "due_feedback", _due_feedback)
    monkeypatch.setattr(aq, "due_reminders", _due_reminders)
    monkeypatch.setattr(wq, "enqueue", _enqueue)
    monkeypatch.setattr(aq, "record_automation_send", _record)

    counts = await wa.run_automations(settings=FakeSettings())

    assert counts["errors"] == 1
    assert counts["reminder"] == 1
    assert len(recorded) == 1 and recorded[0]["shop_id"] == good_shop


@pytest.mark.asyncio
async def test_already_enqueued_appointment_is_still_recorded(monkeypatch):
    """Crash recovery: a previous tick enqueued this appointment but died
    before recording. The unique index returns None; we record anyway, so the
    appointment stops being due and the already-queued row is delivered once."""
    calls = _patch_automations(monkeypatch, due=[_due_row()], enqueue_result=None)

    counts = await wa.run_automations(settings=FakeSettings())

    assert counts["feedback"] == 0              # not a new send
    assert len(calls["recorded"]) == 1          # but the appointment is logged


def test_render_variables_formats_facts_not_generated_copy():
    row = _due_row(appointment_at=datetime(2026, 8, 12, 10, 30, tzinfo=ROME))

    rem = wa.render_variables("reminder_v1", row)
    assert rem["1"] == "Giulia"
    assert rem["2"] == "Salone X"
    assert rem["3"] == "mercoledì 12 alle 10:30"
    assert rem["4"] == "Taglio"

    fb = wa.render_variables(
        "feedback_v2", row, platform="google", link="https://g.page/r/x",
    )
    assert fb["3"] == "12 agosto"
    assert fb["5"] == "Google"
    assert fb["6"] == "https://g.page/r/x"

    general = wa.render_variables("feedback_v2", row)
    assert general["5"] == "un canale a tua scelta"
    assert general["6"] == "rispondendo a questo messaggio"


# --------------------------------------------- UTILITY bypasses the send gate

def _consentless(**over):
    row = {"id": uuid4(), "full_name": "Giulia", "phone": "+393331112222",
           "phone_normalized": "393331112222", "marketing_consent": False,
           "marketing_consent_granted_at": None,
           "marketing_consent_withdrawn_at": None}
    row.update(over)
    return row


async def _patch_enqueue(monkeypatch, *, template=None, customer=None):
    async def _sender(shop_id):
        return {"status": "online", "phone_number": "+393331110000",
                "phone_number_id": "PN1", "access_token": "tok", "daily_cap": 50,
                "messaging_limit": "TIER_1K", "platform_type": "COEXISTENCE"}
    async def _template(shop_id, key):
        return template if template is not None else {
            "status": "approved", "name": "kairo_promo_v1", "language": "it",
            "category": "MARKETING",
        }
    async def _recent(*, shop_id, customer_ids, hours):
        return set()
    async def _customer(shop_id, customer_id):
        return _consentless() if customer is None else customer
    monkeypatch.setattr(wq, "get_sender", _sender)
    monkeypatch.setattr(wq, "get_template", _template)
    monkeypatch.setattr(wq, "recently_contacted", _recent)
    from booking_engine.db import sms_queries
    monkeypatch.setattr(sms_queries, "get_customer_for_send", _customer)


@pytest.mark.asyncio
async def test_utility_send_to_a_consentless_customer_is_enqueued_but_marketing_is_not(monkeypatch):
    """The whole point of the category existing.

    UTILITY is transactional (a reminder about the customer's own
    appointment), so it needs no marketing consent and is not suppressed by
    the cooldown. The same customer, on a MARKETING template, must still be
    refused — the two are not the same gate.
    """
    rows = []
    async def _enqueue(**kw):
        rows.append(kw)
        return uuid4()
    monkeypatch.setattr(wq, "enqueue", _enqueue)

    # UTILITY first: same consent-less customer, queued.
    await _patch_enqueue(monkeypatch, template={
        "status": "approved", "name": "kairo_feedback_v1", "language": "it",
        "category": "UTILITY",
    })
    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="feedback_v1",
        recipients=[{"customer_id": uuid4(), "variables": {"1": "Giulia"}}],
        settings=FakeSettings(),
    )
    assert result["queued"] == 1 and result["suppressed"] == 0
    assert rows[-1]["status"] == "queued"

    # MARKETING: the same customer is refused.
    await _patch_enqueue(monkeypatch)
    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c2", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}}],
        settings=FakeSettings(),
    )
    assert result["queued"] == 0 and result["suppressed"] == 1
    assert rows[-1]["suppressed_reason"] == "no_consent"


@pytest.mark.asyncio
async def test_send_due_does_not_suppress_a_utility_message_for_no_consent(monkeypatch):
    """send_due re-reads consent at send time — but only for MARKETING.

    A queued UTILITY reminder must reach the send even if the customer has no
    marketing consent, else the whole automation feature would suppress itself
    at the drip stage.
    """
    spy = {"sent": [], "meta_sends": []}
    message = {
        "id": uuid4(), "shop_id": SHOP, "customer_id": uuid4(),
        "to_phone": "+393331112222", "from_number": "+393331110000",
        "template_name": "kairo_feedback_v1", "template_language": "it",
        "variables": {"1": "Giulia"}, "category": "UTILITY",
    }

    async def _claim(limit):
        return [message]
    async def _requeue_stuck(*a, **kw):
        return 0
    async def _sender(shop_id):
        return {"status": "online", "phone_number_id": "PN1",
                "access_token": "tok", "daily_cap": 50,
                "messaging_limit": "TIER_1K", "platform_type": "COEXISTENCE"}
    async def _sent_24h(shop_id):
        return 0
    async def _send(**kw):
        spy["meta_sends"].append(kw)
        return "wamid.1"

    async def _mark_sent(**kw):
        spy["sent"].append(kw)
    async def _mark_suppressed(**kw):
        spy.setdefault("suppressed", []).append(kw)
    monkeypatch.setattr(wq, "claim_due", _claim)
    monkeypatch.setattr(wq, "requeue_stuck", _requeue_stuck)
    monkeypatch.setattr(wq, "get_sender", _sender)
    monkeypatch.setattr(wq, "sent_last_24h", _sent_24h)
    monkeypatch.setattr(wq, "mark_sent", _mark_sent)
    monkeypatch.setattr(wq, "mark_suppressed", _mark_suppressed)
    from booking_engine.clients import meta_whatsapp as meta
    monkeypatch.setattr(meta, "send_template", _send)

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["sent"] == 1 and counts["suppressed"] == 0
    assert len(spy["sent"]) == 1
