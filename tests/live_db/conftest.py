"""Live DB test fixtures — real Neon PostgreSQL connection.

These tests run against the actual Neon database.
They are skipped automatically if DATABASE_URL is not set or connection fails.

Run with: DATABASE_URL=postgresql://... pytest tests/live_db/ -v
"""
from __future__ import annotations

import asyncio
import logging
import os
from uuid import UUID

import pytest

from booking_engine.config import Settings
from booking_engine.db import connection

logger = logging.getLogger(__name__)

# ── Seed data IDs from 02_seed_data.sql ──
SHOP_ID = UUID("a0000000-0000-0000-0000-000000000001")
SHOP_ID_2 = UUID("b0000000-0000-0000-0000-000000000002")

STAFF_MIRCO = UUID("11111111-0000-0000-0000-000000000001")
STAFF_GIULIA = UUID("11111111-0000-0000-0000-000000000002")
STAFF_MARCO = UUID("11111111-0000-0000-0000-000000000003")

SVC_TAGLIO_DONNA = UUID("aaaa0001-0000-0000-0000-000000000001")  # 45 min
SVC_TAGLIO_UOMO = UUID("aaaa0001-0000-0000-0000-000000000002")   # 30 min
SVC_COLORE = UUID("aaaa0001-0000-0000-0000-000000000003")        # 60 min
SVC_PIEGA = UUID("aaaa0001-0000-0000-0000-000000000005")         # 30 min

CUSTOMER_MARIA = UUID("cccc0001-0000-0000-0000-000000000001")
CUSTOMER_LUCA = UUID("cccc0001-0000-0000-0000-000000000002")

PHONE_MARIA = "+39 333 1111111"
PHONE_LUCA = "+39 333 2222222"


def _get_db_settings() -> Settings | None:
    """Build Settings from DATABASE_URL environment variable."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None
    return Settings(database_url=db_url)


def _try_connect() -> bool:
    """Quick connectivity check using raw asyncpg (not the app pool)."""
    settings = _get_db_settings()
    if settings is None:
        return False
    try:
        import asyncpg
        loop = asyncio.new_event_loop()
        conn = loop.run_until_complete(asyncpg.connect(dsn=settings.database_url))
        result = loop.run_until_complete(conn.fetchrow("SELECT 1 AS ping"))
        loop.run_until_complete(conn.close())
        loop.close()
        return result is not None
    except Exception as e:
        logger.warning("Live DB connection failed: %s", e)
        return False


_db_available = _try_connect()

pytestmark = pytest.mark.skipif(
    not _db_available,
    reason="Neon PostgreSQL connection unavailable (set DATABASE_URL env var)",
)


@pytest.fixture(autouse=True)
async def db_connection():
    """Create a fresh connection pool for each test, close after."""
    settings = _get_db_settings()
    await connection.init_connection(settings)
    yield connection
    await connection.close_connection()


@pytest.fixture
async def cleanup_customer_ids():
    """Collect customer IDs created during tests for cleanup."""
    ids: list = []
    yield ids
    for cid in ids:
        uid = UUID(cid) if isinstance(cid, str) else cid
        try:
            await connection.execute_void(
                "DELETE FROM phone_contacts WHERE customer_id = $1", uid,
            )
            await connection.execute_void(
                "DELETE FROM customers WHERE id = $1", uid,
            )
        except Exception as e:
            logger.warning("Cleanup failed for customer %s: %s", cid, e)


@pytest.fixture
async def cleanup_appointment_ids():
    """Collect appointment IDs created during tests for cleanup."""
    ids: list = []
    yield ids
    for aid in ids:
        uid = UUID(aid) if isinstance(aid, str) else aid
        try:
            await connection.execute_void(
                "DELETE FROM appointment_services WHERE appointment_id = $1", uid,
            )
            await connection.execute_void(
                "DELETE FROM appointments WHERE id = $1", uid,
            )
        except Exception as e:
            logger.warning("Cleanup failed for appointment %s: %s", aid, e)
