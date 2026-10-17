"""Live DB test — `wa_session_queries.session_transcript` runs against the real
schema: the UNION with the outreach branch and the `interval` arithmetic on a
bound timestamp are exactly what a fake cannot check.

Commits a throwaway shop and rows through the app pool and deletes them in
`finally`, scoped to that shop id. Non-prod only (conftest resolves the DB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from booking_engine.db import connection
from booking_engine.db import wa_session_queries as ws

PHONE = "+393331110077"


class TestSessionTranscriptLive:
    async def test_runs_and_labels_outreach(self, db_connection):
        pool = connection._get_pool()
        async with pool.acquire() as conn:
            shop_id = await conn.fetchval(
                "INSERT INTO business_app_core.shops (name) "
                "VALUES ('session_transcript test') RETURNING id"
            )
            try:
                since = datetime.now(timezone.utc)
                # Nothing for this number yet: must be [] and, above all, no SQL error.
                assert await ws.session_transcript(
                    shop_id=shop_id, phone=PHONE, since=since) == []

                # A campaign send 20h before the session opened is still in.
                await conn.execute(
                    "INSERT INTO whatsapp.outbound_messages "
                    "(shop_id, from_number, to_phone, preview, status, template_name, campaign_key, sent_at) "
                    "VALUES ($1, '+393339990000', '393331110077', 'Ciao Giulia', 'sent', 'it_gap_fill_v1', "
                    "        'gap_fill:x:2026-10-20T15:00', $2)",
                    shop_id, since - timedelta(hours=20),
                )
                rows = await ws.session_transcript(shop_id=shop_id, phone=PHONE, since=since)
                assert len(rows) == 1
                assert rows[0]["role"] == "assistant"
                assert rows[0]["content"].startswith("[Jev outreach")
                assert rows[0]["content"].endswith("Ciao Giulia")
            finally:
                await conn.execute(
                    "DELETE FROM whatsapp.outbound_messages WHERE shop_id = $1", shop_id)
                await conn.execute(
                    "DELETE FROM business_app_core.shops WHERE id = $1", shop_id)
