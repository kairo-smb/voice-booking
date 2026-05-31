"""asyncpg pool helpers for the voice gateway."""
from __future__ import annotations

from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool(database_url: str, min_size: int = 1, max_size: int = 4) -> None:
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=min_size, max_size=max_size)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("voice_gateway DB pool not initialized")
    return _pool


async def execute(query: str, *args: Any) -> list[dict]:
    async with _require_pool().acquire() as conn:
        rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]


async def execute_one(query: str, *args: Any) -> dict | None:
    async with _require_pool().acquire() as conn:
        row = await conn.fetchrow(query, *args)
        return dict(row) if row else None


async def execute_void(query: str, *args: Any) -> None:
    async with _require_pool().acquire() as conn:
        await conn.execute(query, *args)
