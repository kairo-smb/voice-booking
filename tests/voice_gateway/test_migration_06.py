"""Schema shape assertions for the voice_tones migration (06).

Integration tests that require a live DATABASE_URL.
Run with: DATABASE_URL=<url> pytest tests/voice_gateway/test_migration_06.py -v
"""
from __future__ import annotations

import pytest

from booking_engine.config import Settings
from booking_engine.db.connection import close_connection, execute, execute_one, init_connection


@pytest.fixture(autouse=True)
async def _db():
    settings = Settings()
    if not settings.database_url:
        pytest.skip("DATABASE_URL not set")
    await init_connection(settings)
    yield
    await close_connection()


@pytest.mark.asyncio
async def test_voice_tones_table_exists_with_expected_columns():
    cols = await execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='voice_agent' AND table_name='voice_tones'"
    )
    names = {c["column_name"] for c in cols}
    assert {"id", "name", "description", "system_prompt_instruction",
            "is_preset", "created_at", "created_by_shop_id"} <= names


@pytest.mark.asyncio
async def test_shop_config_has_tone_id_not_tone_preset():
    cols = await execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='voice_agent' AND table_name='shop_config' "
        "AND column_name IN ('tone_id','tone_preset')"
    )
    names = {c["column_name"] for c in cols}
    assert "tone_id" in names
    assert "tone_preset" not in names


@pytest.mark.asyncio
async def test_eight_presets_seeded():
    row = await execute_one(
        "SELECT count(*)::int AS n FROM voice_agent.voice_tones WHERE is_preset = true"
    )
    assert row["n"] == 8
