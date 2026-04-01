"""PostgreSQL (Neon) connection management via asyncpg."""
from __future__ import annotations

import logging

import asyncpg

from booking_engine.config import Settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Connection pool not initialized. Call init_connection first.")
    return _pool


async def execute(sql: str, *args) -> list[dict]:
    """Execute SQL and return all rows as list of dicts."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(row) for row in rows]


async def execute_one(sql: str, *args) -> dict | None:
    """Execute SQL and return one row as dict, or None."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None


async def execute_void(sql: str, *args) -> None:
    """Execute SQL that returns nothing (INSERT/UPDATE/DELETE)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql, *args)


async def init_connection(settings: Settings) -> None:
    """Create the asyncpg connection pool."""
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    logger.info("PostgreSQL connection pool initialized (min=%d, max=%d)",
                settings.pool_min_size, settings.pool_max_size)


async def close_connection() -> None:
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL connection pool closed")
