"""DB access for voice_agent.shop_config (Layer 1)."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import (
    execute, execute_one, execute_one as _exec, execute_void,
)


async def get_policy(locale: str = "it-IT") -> dict | None:
    return await _exec(
        "SELECT * FROM voice_agent.system_policy WHERE locale = $1",
        locale,
    )


async def get_config(shop_id: UUID) -> dict | None:
    return await execute_one(
        "SELECT * FROM voice_agent.shop_config WHERE shop_id = $1",
        shop_id,
    )


async def upsert_config(shop_id: UUID, **fields) -> dict:
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(f"${i+2}" for i in range(len(fields)))
    sets = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields.keys())
    sql = (
        f"INSERT INTO voice_agent.shop_config (shop_id, {cols}, updated_at) "
        f"VALUES ($1, {placeholders}, now()) "
        f"ON CONFLICT (shop_id) DO UPDATE SET {sets}, updated_at = now() "
        f"RETURNING *"
    )
    return await execute_one(sql, shop_id, *fields.values())


# --------------------------------------------------- the low-credit email episode

async def list_credit_notice_candidates() -> list[dict]:
    """Shops the low-credit email sweep must look at.

    Two ways in. An opted-in shop with a live sender is one whose responder a
    low basket would pause — the only shop the email is about. And any shop
    already stamped, whatever its state now, so an episode can always be
    closed: a shop that switched the agent off mid-episode must not carry a
    stale stamp into the next one.
    """
    return await execute(
        """
        SELECT c.shop_id,
               c.credit_low_notified_at,
               (c.whatsapp_agent_enabled AND s.status = 'online') AS eligible
          FROM voice_agent.shop_config c
          LEFT JOIN whatsapp.senders s ON s.shop_id = c.shop_id
         WHERE (c.whatsapp_agent_enabled AND s.status = 'online')
            OR c.credit_low_notified_at IS NOT NULL
        """,
    )


async def claim_credit_low_notice(shop_id: UUID) -> bool:
    """Open the episode atomically. True only for the caller whose UPDATE won.

    Claim first, mail second: two ticks (two Fly machines, or an overlapping
    cron) that both read the row unstamped must never both mail. The
    conditional UPDATE is the lock — Postgres re-checks `IS NULL` on the row
    the second writer waits on, so exactly one gets a row back. A mail that
    then fails to get an answer releases the claim (`set_credit_low_notified`
    with notified=False) so the next tick retries.
    """
    row = await execute_one(
        """
        UPDATE voice_agent.shop_config
           SET credit_low_notified_at = now()
         WHERE shop_id = $1
           AND credit_low_notified_at IS NULL
        RETURNING 1
        """,
        shop_id,
    )
    return row is not None


async def set_credit_low_notified(shop_id: UUID, *, notified: bool) -> None:
    """Open the episode (stamp now) or close it (NULL)."""
    await execute_void(
        """
        UPDATE voice_agent.shop_config
           SET credit_low_notified_at = CASE WHEN $2 THEN now() END
         WHERE shop_id = $1
        """,
        shop_id, notified,
    )
