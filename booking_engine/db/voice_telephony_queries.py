"""DB access for voice_agent.shop_telephony."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute_one


async def upsert_telephony(
    *,
    shop_id: UUID,
    provider: str,
    kairo_number: str,
    kairo_number_sid: str,
    salon_existing_number: str | None,
    setup_path: str,
    activation_status: str = "active",
) -> dict:
    return await execute_one(
        """
        INSERT INTO voice_agent.shop_telephony
            (shop_id, provider, kairo_number, kairo_number_sid,
             salon_existing_number, setup_path, activation_status)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        ON CONFLICT (shop_id) DO UPDATE SET
            provider = EXCLUDED.provider,
            kairo_number = EXCLUDED.kairo_number,
            kairo_number_sid = EXCLUDED.kairo_number_sid,
            salon_existing_number = EXCLUDED.salon_existing_number,
            setup_path = EXCLUDED.setup_path,
            activation_status = EXCLUDED.activation_status,
            provisioned_at = now()
        RETURNING *
        """,
        shop_id, provider, kairo_number, kairo_number_sid,
        salon_existing_number, setup_path, activation_status,
    )



async def get_telephony(shop_id: UUID) -> dict | None:
    return await execute_one(
        "SELECT * FROM voice_agent.shop_telephony WHERE shop_id = $1",
        shop_id,
    )


async def get_telephony_by_kairo_number(kairo_number: str) -> dict | None:
    return await execute_one(
        "SELECT * FROM voice_agent.shop_telephony WHERE kairo_number = $1",
        kairo_number,
    )
