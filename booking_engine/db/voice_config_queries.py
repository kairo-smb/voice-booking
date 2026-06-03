"""DB access for voice_agent.shop_config (Layer 1)."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute_one


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
