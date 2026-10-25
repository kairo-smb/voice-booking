"""Live DB test — `whatsapp_queries.in_conversation` matches a number however
the inbound row spelled it, and honours the window.

Unlike its neighbours this cannot run inside a rolled-back transaction: the
query goes through the app pool, a different connection that would never see
uncommitted rows. So it commits a throwaway shop and its rows and deletes them
in `finally`. Runs only against the non-prod database conftest resolves.
"""
from __future__ import annotations

from datetime import timedelta

from booking_engine.db import connection
from booking_engine.db import whatsapp_queries as wq


class TestInConversation:
    async def test_matches_without_plus_and_respects_the_window(self, db_connection):
        pool = connection._get_pool()
        async with pool.acquire() as conn:
            shop_id = await conn.fetchval(
                "INSERT INTO business_app_core.shops (name) "
                "VALUES ('in_conversation test') RETURNING id"
            )
            try:
                # Stored without '+', one inside the window and one outside.
                await conn.execute(
                    "INSERT INTO whatsapp.inbound_messages (shop_id, from_phone, received_at) "
                    "VALUES ($1, '393331110001', now() - interval '2 hours'), "
                    "       ($1, '393331110002', now() - interval '30 hours')",
                    shop_id,
                )
                got = await wq.in_conversation(
                    shop_id=shop_id,
                    phones=["+393331110001", "+393331110002", "+393331110003"],
                    within=timedelta(hours=24),
                )
                assert got == {"+393331110001"}
            finally:
                await conn.execute(
                    "DELETE FROM whatsapp.inbound_messages WHERE shop_id = $1", shop_id)
                await conn.execute(
                    "DELETE FROM business_app_core.shops WHERE id = $1", shop_id)
