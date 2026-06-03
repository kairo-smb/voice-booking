"""Smoke tests for migration 04 — confirms columns and tables exist after migrate.

These are integration tests that require a live DATABASE_URL.
Run with: DATABASE_URL=<url> pytest tests/voice_gateway/test_migration_04.py -v
"""
from __future__ import annotations

import os

import pytest

from booking_engine.db.connection import execute, execute_one, init_connection, close_connection
from booking_engine.config import Settings


@pytest.fixture(autouse=True)
async def _db():
    settings = Settings()
    if not settings.database_url:
        pytest.skip("DATABASE_URL not set")
    await init_connection(settings)
    yield
    await close_connection()


@pytest.mark.asyncio
async def test_customers_has_source_column():
    rows = await execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='business_app_core' AND table_name='customers' "
        "AND column_name='source'"
    )
    assert len(rows) > 0


@pytest.mark.asyncio
async def test_customers_has_phone_normalized_column():
    rows = await execute(
        "SELECT column_name, generation_expression FROM information_schema.columns "
        "WHERE table_schema='business_app_core' AND table_name='customers' "
        "AND column_name='phone_normalized'"
    )
    assert len(rows) > 0


@pytest.mark.asyncio
async def test_customers_phone_normalized_index_exists():
    rows = await execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname='business_app_core' "
        "AND indexname='customers_shop_phone_normalized_idx'"
    )
    assert len(rows) > 0


@pytest.mark.asyncio
async def test_appointments_has_source_and_voice_call_id():
    rows = await execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='business_app_core' AND table_name='appointments' "
        "AND column_name IN ('source', 'voice_call_id', 'confirmation_status')"
    )
    names = {r['column_name'] for r in rows}
    assert {'source', 'voice_call_id', 'confirmation_status'}.issubset(names)


@pytest.mark.asyncio
async def test_voice_agent_callback_memos_exists():
    rows = await execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='voice_agent' AND table_name='callback_memos'"
    )
    assert len(rows) > 0


@pytest.mark.asyncio
async def test_voice_agent_shop_telephony_exists():
    rows = await execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='voice_agent' AND table_name='shop_telephony'"
    )
    assert len(rows) > 0


@pytest.mark.asyncio
async def test_voice_agent_shop_config_has_fallback_columns():
    rows = await execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='voice_agent' AND table_name='shop_config' "
        "AND column_name IN ('manual_fallback_number', 'manual_fallback_normalized', "
        "'auto_topup_enabled', 'auto_topup_threshold_tokens', 'enabled')"
    )
    names = {r['column_name'] for r in rows}
    assert {'manual_fallback_number', 'manual_fallback_normalized',
            'auto_topup_enabled', 'auto_topup_threshold_tokens', 'enabled'}.issubset(names)
