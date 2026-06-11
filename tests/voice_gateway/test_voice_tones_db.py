"""DB layer for voice_agent.voice_tones.

Integration tests against a live DATABASE_URL (skipped otherwise).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from booking_engine.config import Settings
from booking_engine.db.connection import close_connection, init_connection
from booking_engine.db.voice_tone_queries import (
    get_tone_by_id,
    list_preset_tones,
)


@pytest.fixture(autouse=True)
async def _db():
    settings = Settings()
    if not settings.database_url:
        pytest.skip("DATABASE_URL not set")
    await init_connection(settings)
    yield
    await close_connection()


@pytest.mark.asyncio
async def test_list_preset_tones_returns_eight_seeded():
    tones = await list_preset_tones()
    assert len(tones) == 8
    assert all(t["is_preset"] is True for t in tones)
    names = {t["name"] for t in tones}
    assert {"professionale", "amichevole", "efficiente", "luxury",
            "tecnico", "casual", "empatico", "conciso"} <= names


@pytest.mark.asyncio
async def test_get_tone_by_id_returns_full_row():
    presets = await list_preset_tones()
    target = presets[0]
    fetched = await get_tone_by_id(target["id"])
    assert fetched is not None
    assert fetched["name"] == target["name"]
    assert fetched["system_prompt_instruction"]


@pytest.mark.asyncio
async def test_get_tone_by_id_missing_returns_none():
    assert await get_tone_by_id(uuid4()) is None
