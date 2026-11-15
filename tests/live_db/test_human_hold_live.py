"""Live DB test — a session a person answered in closes HUMAN_HOLD after their
last reply, so the customer's next message opens a fresh one the agent answers
(owner decision, 2026-11-15). The rule is SQL inside `open_session`'s lookup,
which a fake cannot check.

Commits a throwaway shop and deletes it in `finally`, scoped to that shop id.
Non-prod only (conftest resolves the DB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from booking_engine.db import connection
from booking_engine.db import wa_session_queries as ws

PHONE = "+393331110088"


class TestHumanHoldLive:
    async def test_the_agent_gets_the_thread_back_three_hours_after_the_human(
        self, db_connection,
    ):
        pool = connection._get_pool()
        async with pool.acquire() as conn:
            shop_id = await conn.fetchval(
                "INSERT INTO business_app_core.shops (name) "
                "VALUES ('human_hold test') RETURNING id"
            )
            now = datetime.now(timezone.utc)

            async def reply(ago: timedelta, origin: str, sid: str | None = None):
                await conn.execute(
                    "INSERT INTO whatsapp.outbound_messages "
                    "(shop_id, from_number, to_phone, preview, status, origin, sent_at, provider_sid) "
                    "VALUES ($1, '+393339990000', $2, 'x', 'sent', $3, $4, $5)",
                    shop_id, PHONE, origin, now - ago, sid,
                )

            # A message that arrives now: the reused session keeps its own
            # start (5h back, set on the first call); a new one starts here.
            async def session(started_at=None):
                return await ws.open_session(
                    shop_id=shop_id, phone=PHONE, customer_id=None,
                    started_at=started_at or now,
                )

            try:
                first = await session(now - timedelta(hours=5))
                # No person has written: the session is reused.
                assert await session() == first
                # The agent's own reply, and its echo on the owner's phone, are
                # not a person answering.
                await reply(timedelta(hours=1), "agent", "wamid.A")
                await reply(timedelta(minutes=10), "phone", "wamid.A")
                assert await session() == first
                # A person wrote 2h59 ago: still theirs.
                await reply(timedelta(hours=2, minutes=59), "kairo")
                assert await session() == first
                state = await ws.session_state(call_id=first, phone=PHONE)
                assert state["human_replied_at"] is not None
                # Past the hold: the next message opens a new session, clean.
                await conn.execute(
                    "UPDATE whatsapp.outbound_messages SET sent_at = $2 "
                    "WHERE shop_id = $1 AND origin = 'kairo'",
                    shop_id, now - timedelta(hours=3, minutes=1),
                )
                second = await session()
                assert second != first
                state = await ws.session_state(call_id=second, phone=PHONE)
                assert state["human_replied_at"] is None
                assert not state["escalated"]
            finally:
                await conn.execute(
                    "DELETE FROM voice_agent.calls WHERE shop_id = $1", shop_id)
                await conn.execute(
                    "DELETE FROM whatsapp.outbound_messages WHERE shop_id = $1", shop_id)
                await conn.execute(
                    "DELETE FROM business_app_core.shops WHERE id = $1", shop_id)
