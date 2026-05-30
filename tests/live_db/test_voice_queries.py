"""Live-DB tests for voice_agent queries."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from booking_engine.db.connection import init_connection, close_connection, execute_void
from booking_engine.config import Settings
from booking_engine.db import voice_queries as vq

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for live-DB tests",
)


@pytest.fixture(scope="module", autouse=True)
async def db_pool():
    await init_connection(Settings(database_url=os.environ["DATABASE_URL"]))
    yield
    await close_connection()


@pytest.fixture
async def seeded_shop():
    shop_id = uuid4()
    await execute_void(
        "INSERT INTO business_app_core.shops (id, name, is_active) "
        "VALUES ($1, 'Test Shop', true)",
        shop_id,
    )
    yield shop_id
    await execute_void("DELETE FROM voice_agent.calls WHERE shop_id = $1", shop_id)
    await execute_void("DELETE FROM business_app_core.shops WHERE id = $1", shop_id)


async def test_get_voice_config_returns_defaults(seeded_shop):
    cfg = await vq.get_voice_config(seeded_shop)
    assert cfg["voice"] == "alloy"
    assert cfg["language"] == "it"
    assert cfg["is_active"] is True


async def test_get_voice_config_missing_shop_returns_none():
    cfg = await vq.get_voice_config(uuid4())
    assert cfg is None


async def test_update_voice_config_partial(seeded_shop):
    updated = await vq.update_voice_config(
        seeded_shop, {"welcome_message": "Ciao!", "voice": "echo"}
    )
    assert updated["welcome_message"] == "Ciao!"
    assert updated["voice"] == "echo"
    assert updated["language"] == "it"  # untouched


async def test_update_voice_config_empty_payload_is_noop(seeded_shop):
    before = await vq.get_voice_config(seeded_shop)
    after = await vq.update_voice_config(seeded_shop, {})
    assert before == after
