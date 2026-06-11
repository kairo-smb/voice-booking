"""DB access for voice_agent.voice_tones."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute, execute_one


async def list_preset_tones() -> list[dict]:
    return await execute(
        "SELECT id, name, description, system_prompt_instruction, "
        "is_preset, created_at, created_by_shop_id "
        "FROM voice_agent.voice_tones WHERE is_preset = true ORDER BY name"
    )


async def get_tone_by_id(tone_id: UUID) -> dict | None:
    return await execute_one(
        "SELECT id, name, description, system_prompt_instruction, "
        "is_preset, created_at, created_by_shop_id "
        "FROM voice_agent.voice_tones WHERE id = $1",
        tone_id,
    )
