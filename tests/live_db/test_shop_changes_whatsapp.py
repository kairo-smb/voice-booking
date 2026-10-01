"""Live DB test — migration 29's triggers on `whatsapp.outbound_messages` and
`whatsapp.inbound_messages` actually bump the webapp's shop_changes counter.

Guarded the same way the migration itself is: `bump_shop_changes()` is the
webapp's function (its migration 73), so on a branch that doesn't have it yet
— this repo's own CI branch, forked off production before the webapp's
migration has run there — this test has nothing to verify and skips rather
than erroring.

Self-contained like test_sms_live.py: creates its own throwaway shop rather
than relying on conftest's seeded fixture ids (not guaranteed present on
every branch this suite runs against), and does all of its writes inside one
transaction that is rolled back at the end — nothing here is left behind
regardless of which (non-prod) database TEST_DATABASE_URL points at.

Run with:  TEST_DATABASE_URL=postgresql://... pytest tests/live_db/test_shop_changes_whatsapp.py -v
"""
from __future__ import annotations

import pytest

from booking_engine.db import connection


async def _whatsapp_version(conn, shop_id) -> int:
    row = await conn.fetchrow(
        """
        SELECT version FROM business_app_core.shop_changes
        WHERE shop_id = $1 AND domain = 'whatsapp'
        """,
        shop_id,
    )
    return row["version"] if row else 0


class TestShopChangesWhatsAppTrigger:
    async def test_outbound_and_inbound_writes_bump_the_whatsapp_counter(self, db_connection):
        pool = connection._get_pool()
        async with pool.acquire() as conn:
            has_bump_fn = await conn.fetchval(
                "SELECT to_regprocedure('business_app_core.bump_shop_changes()')"
            )
            if has_bump_fn is None:
                pytest.skip(
                    "webapp migration 73 (bump_shop_changes) not applied on this branch"
                )

            tr = conn.transaction()
            await tr.start()
            try:
                shop_id = await conn.fetchval(
                    """
                    INSERT INTO business_app_core.shops (name)
                    VALUES ('live-updates whatsapp trigger test')
                    RETURNING id
                    """
                )

                # One statement, three rows: the trigger is statement-level with a
                # transition table, so this must bump the counter exactly once,
                # not three times.
                await conn.execute(
                    """
                    INSERT INTO whatsapp.outbound_messages (shop_id, to_phone, from_number)
                    SELECT $1, '+3933300000' || g, '+393339990000'
                    FROM generate_series(1, 3) g
                    """,
                    shop_id,
                )
                assert await _whatsapp_version(conn, shop_id) == 1

                await conn.execute(
                    """
                    INSERT INTO whatsapp.inbound_messages (shop_id, from_phone)
                    VALUES ($1, '+393331112222')
                    """,
                    shop_id,
                )
                assert await _whatsapp_version(conn, shop_id) == 2
            finally:
                await tr.rollback()
