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


async def _retry_stale_plan(op):
    """Run `op(conn)`, once more if a migration changed a cached statement's result type.

    asyncpg caches prepared statements per connection; `RETURNING m.*` (or
    `SELECT *`) then fails with InvalidCachedStatementError after a column is
    added, on every pooled connection, until the process restarts. asyncpg
    drops the stale statement when it raises, so one retry re-prepares it.
    """
    pool = _get_pool()
    for attempt in (0, 1):
        try:
            async with pool.acquire() as conn:
                return await op(conn)
        except asyncpg.exceptions.InvalidCachedStatementError:
            if attempt:
                raise
            logger.warning("stale cached statement after a schema change; retrying once")


async def execute(sql: str, *args) -> list[dict]:
    """Execute SQL and return all rows as list of dicts."""
    rows = await _retry_stale_plan(lambda c: c.fetch(sql, *args))
    return [dict(row) for row in rows]


async def execute_one(sql: str, *args) -> dict | None:
    """Execute SQL and return one row as dict, or None."""
    row = await _retry_stale_plan(lambda c: c.fetchrow(sql, *args))
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
        # Live Neon DB keeps base tables in business_app_core; set search_path
        # so unqualified table references in queries.py resolve correctly.
        server_settings={"search_path": "business_app_core, public"},
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
