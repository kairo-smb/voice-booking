"""`customer_campaign_messages` — the GDPR subject-access artifact.

Real Postgres, not a mock: the function issues a single three-branch
`UNION ALL` with hand-typed NULLs, and this repo has shipped that exact class
of bug twice before (AGENTS.md 2026-07-18, 2026-07-21 — an untyped NULL or a
mistyped interval that only Postgres itself catches). A mocked `execute()`
would happily accept a query that fails to plan.

Runs against a local scratch database (`wa_scratch` by default, overridable
via `SUBJECT_ACCESS_TEST_DB_URL`) that already carries the `whatsapp` schema
through migration 24 and stub `business_app_core`/`market_intel` tables —
built the same way earlier tasks in this plan built theirs. Skips cleanly
when that database isn't reachable, the same posture as
`test_whatsapp_automations.py`'s integration class, and deliberately its own
env var rather than `TEST_DATABASE_URL`/`DATABASE_URL`: those already gate a
different integration class in this test session, against a schema that
doesn't carry the tables this file needs, and sharing the name would flip
that class's skip decision as a side effect of this one.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from booking_engine.config import Settings
from booking_engine.db import connection
from booking_engine.db import whatsapp_queries as wq


def _resolve_test_db_url() -> str:
    return os.environ.get("SUBJECT_ACCESS_TEST_DB_URL", "postgresql:///wa_scratch")


def _try_connect() -> bool:
    try:
        import asyncio

        import asyncpg

        loop = asyncio.new_event_loop()
        conn = loop.run_until_complete(asyncpg.connect(dsn=_resolve_test_db_url()))
        ok = loop.run_until_complete(conn.fetchrow("SELECT 1 AS ping")) is not None
        loop.run_until_complete(conn.close())
        loop.close()
        return ok
    except Exception:
        return False


_db_available = _try_connect()

pytestmark = pytest.mark.skipif(
    not _db_available,
    reason="Scratch DB unavailable: set SUBJECT_ACCESS_TEST_DB_URL, or build "
           "a local `wa_scratch` database with migrations 14-24 applied "
           "plus market_intel.campaigns/campaign_recipients.",
)


@pytest.fixture
async def db_connection():
    settings = Settings(database_url=_resolve_test_db_url())
    await connection.init_connection(settings)
    yield connection
    await connection.close_connection()


# --------------------------------------------------------------------- helpers

async def _insert_shop(shop_id):
    await connection.execute_void(
        "INSERT INTO business_app_core.shops (id) VALUES ($1)", shop_id,
    )


async def _insert_customer(shop_id, customer_id):
    await connection.execute_void(
        "INSERT INTO business_app_core.customers (id, shop_id) VALUES ($1, $2)",
        customer_id, shop_id,
    )


async def _insert_outbound(
    *, shop_id, customer_id, preview="ciao", campaign_key=None,
    status="sent", sent_at=None, created_at=None,
):
    row_id = uuid4()
    await connection.execute_void(
        """
        INSERT INTO whatsapp.outbound_messages
            (id, shop_id, customer_id, campaign_key, to_phone, from_number,
             preview, status, sent_at, created_at)
        VALUES ($1,$2,$3,$4,'+391','+392',$5,$6,$7,coalesce($8, now()))
        """,
        row_id, shop_id, customer_id, campaign_key, preview, status,
        sent_at, created_at,
    )
    return row_id


async def _insert_inbound(
    *, shop_id, customer_id, body="", transcript=None, received_at=None,
):
    row_id = uuid4()
    await connection.execute_void(
        """
        INSERT INTO whatsapp.inbound_messages
            (id, shop_id, customer_id, from_phone, body, transcript, received_at)
        VALUES ($1,$2,$3,'+391',$4,$5,coalesce($6, now()))
        """,
        row_id, shop_id, customer_id, body, transcript, received_at,
    )
    return row_id


async def _insert_campaign(*, shop_id, campaign_id=None, goal="Promo autunno"):
    campaign_id = campaign_id or uuid4()
    await connection.execute_void(
        """
        INSERT INTO market_intel.campaigns
            (id, shop_id, goal, audience_spec, audience_sql)
        VALUES ($1,$2,$3,'{}'::jsonb,'select 1')
        """,
        campaign_id, shop_id, goal,
    )
    return campaign_id


async def _insert_recipient(*, campaign_id, customer_id, arm="holdout", preview=""):
    await connection.execute_void(
        """
        INSERT INTO market_intel.campaign_recipients
            (campaign_id, customer_id, arm, preview)
        VALUES ($1,$2,$3,$4)
        """,
        campaign_id, customer_id, arm, preview,
    )


@pytest.fixture
async def shop(db_connection):
    """A fresh shop + customer, cleaned up after — this is a shared scratch DB."""
    shop_id, customer_id = uuid4(), uuid4()
    await _insert_shop(shop_id)
    await _insert_customer(shop_id, customer_id)
    yield shop_id, customer_id
    await connection.execute_void(
        "DELETE FROM whatsapp.inbound_messages WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM whatsapp.outbound_messages WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM market_intel.campaign_recipients WHERE customer_id = $1",
        customer_id,
    )
    await connection.execute_void(
        "DELETE FROM market_intel.campaigns WHERE shop_id = $1", shop_id,
    )
    await connection.execute_void(
        "DELETE FROM business_app_core.customers WHERE id = $1", customer_id,
    )
    await connection.execute_void(
        "DELETE FROM business_app_core.shops WHERE id = $1", shop_id,
    )


# ----------------------------------------------------------------------- tests

async def test_the_gdpr_artifact_includes_what_the_customer_wrote(shop):
    shop_id, customer_id = shop
    await _insert_inbound(shop_id=shop_id, customer_id=customer_id, body="vorrei disdire")

    rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)

    assert any(r["direction"] == "in" and r["body"] == "vorrei disdire" for r in rows)


async def test_a_voice_note_appears_as_its_transcript_not_an_empty_row(shop):
    """No transcript yet -> the raw (empty) body. A transcript arrives later
    and must replace what the artifact shows, never leave it blank."""
    shop_id, customer_id = shop
    await _insert_inbound(shop_id=shop_id, customer_id=customer_id, body="")

    rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)
    inbound = [r for r in rows if r["direction"] == "in"]
    assert len(inbound) == 1
    assert inbound[0]["body"] == ""

    await _insert_inbound(
        shop_id=shop_id, customer_id=customer_id, body="",
        transcript="vorrei prenotare per venerdì",
    )
    rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)
    transcribed = [r for r in rows if r["direction"] == "in" and r["body"]]
    assert transcribed and transcribed[0]["body"] == "vorrei prenotare per venerdì"


async def test_outbound_rows_still_come_back_tagged_direction_out(shop):
    """No regression: everything the pre-existing artifact carried survives,
    with only `direction`/`body` added."""
    shop_id, customer_id = shop
    campaign_id = await _insert_campaign(shop_id=shop_id, goal="Ritorno clienti")
    await _insert_outbound(
        shop_id=shop_id, customer_id=customer_id, preview="Ciao Giulia!",
        campaign_key=str(campaign_id), status="delivered",
    )

    rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)

    out_rows = [r for r in rows if r["direction"] == "out"]
    assert len(out_rows) == 1
    row = out_rows[0]
    assert row["arm"] == "send"
    assert row["preview"] == "Ciao Giulia!"
    assert row["body"] == "Ciao Giulia!"
    assert row["delivery_status"] == "delivered"
    assert row["campaign_key"] == str(campaign_id)
    assert row["goal"] == "Ritorno clienti"
    assert row["message_id"] is not None
    # The webapp's status veil reads these: when a queued row will leave, and
    # what Meta said when one failed.
    assert row["scheduled_at"] is not None
    assert row["error_code"] is None


async def test_holdout_campaigns_are_still_returned(shop):
    """Untouched by this change: a customer assigned to a campaign's holdout
    arm still shows up, even though nothing was ever sent to them."""
    shop_id, customer_id = shop
    campaign_id = await _insert_campaign(shop_id=shop_id, goal="VIP autunno")
    await _insert_recipient(
        campaign_id=campaign_id, customer_id=customer_id, arm="holdout",
        preview="offerta mai inviata",
    )

    rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)

    holdouts = [r for r in rows if r["arm"] == "holdout"]
    assert len(holdouts) == 1
    assert holdouts[0]["goal"] == "VIP autunno"
    assert holdouts[0]["preview"] == "offerta mai inviata"
    assert holdouts[0]["message_id"] is None


async def test_combined_result_is_ordered_by_time_across_both_directions(shop):
    shop_id, customer_id = shop
    now = datetime.now(timezone.utc)
    await _insert_outbound(
        shop_id=shop_id, customer_id=customer_id, preview="prima",
        created_at=now - timedelta(hours=3), sent_at=now - timedelta(hours=3),
    )
    await _insert_inbound(
        shop_id=shop_id, customer_id=customer_id, body="seconda",
        received_at=now - timedelta(hours=2),
    )
    await _insert_outbound(
        shop_id=shop_id, customer_id=customer_id, preview="terza",
        created_at=now - timedelta(hours=1), sent_at=now - timedelta(hours=1),
    )

    rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)

    bodies_newest_first = [r["body"] for r in rows]
    assert bodies_newest_first == ["terza", "seconda", "prima"]


async def test_an_inbound_row_for_a_different_customer_does_not_leak(shop):
    shop_id, customer_id = shop
    other_customer = uuid4()
    await _insert_customer(shop_id, other_customer)
    try:
        await _insert_inbound(
            shop_id=shop_id, customer_id=other_customer, body="messaggio di un altro",
        )

        rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)

        assert all(r["body"] != "messaggio di un altro" for r in rows)
    finally:
        await connection.execute_void(
            "DELETE FROM whatsapp.inbound_messages WHERE customer_id = $1",
            other_customer,
        )
        await connection.execute_void(
            "DELETE FROM business_app_core.customers WHERE id = $1", other_customer,
        )


async def test_a_customer_with_only_inbound_and_no_outbound_still_gets_a_result(shop):
    shop_id, customer_id = shop
    await _insert_inbound(shop_id=shop_id, customer_id=customer_id, body="solo io ho scritto")

    rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)

    assert len(rows) == 1
    assert rows[0]["direction"] == "in"
    assert rows[0]["body"] == "solo io ho scritto"
