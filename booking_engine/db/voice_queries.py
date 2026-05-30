"""SQL query functions for voice_agent schema and shops.voice/language columns."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute, execute_one, execute_void


_VOICE_CONFIG_FIELDS = (
    "welcome_message", "tone_instructions", "personality", "special_instructions",
    "voice", "language", "is_active",
)
_ALLOWED_UPDATE_FIELDS = set(_VOICE_CONFIG_FIELDS)


async def get_voice_config(shop_id: UUID) -> dict | None:
    cols = ", ".join(_VOICE_CONFIG_FIELDS)
    return await execute_one(
        f"SELECT {cols} FROM business_app_core.shops WHERE id = $1",
        shop_id,
    )


async def update_voice_config(shop_id: UUID, patch: dict) -> dict | None:
    fields = [(k, v) for k, v in patch.items() if k in _ALLOWED_UPDATE_FIELDS]
    if not fields:
        return await get_voice_config(shop_id)
    set_clause = ", ".join(f"{k} = ${i+2}" for i, (k, _) in enumerate(fields))
    values = [v for _, v in fields]
    await execute_void(
        f"UPDATE business_app_core.shops SET {set_clause} WHERE id = $1",
        shop_id, *values,
    )
    return await get_voice_config(shop_id)
