"""In-process scheduler: the jobs GitHub's cron used to trigger over HTTP
(throttled to every few hours in practice).

Every machine runs the same loops, so each job is a cluster-wide singleton via
a Postgres advisory lock: whoever gets it runs, the rest skip that round
(try-lock, never wait). That makes running N machines safe — the lock, not the
machine count, decides who works.

Jobs are aligned to the wall clock (a 3600s job fires at :00) so every
machine's attempt lands at the same moment and all but one skip. Without the
alignment, N machines with drifting timers would each run the job once per
interval — N runs an hour instead of one.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from booking_engine.db.connection import _get_pool

logger = logging.getLogger(__name__)

# Advisory lock keys — arbitrary, but fixed forever: two releases running side
# by side during a deploy must agree on them.
DRAIN_LOCK = 7_301_001
TICK_LOCK = 7_301_002
HEARTBEAT_LOCK = 7_301_003


@asynccontextmanager
async def singleton(key: int):
    """Yield True if this process holds `key` cluster-wide, False if another
    does.

    Transaction-level, not session-level: DATABASE_URL goes through Neon's
    pgbouncer in transaction mode, where a session lock can land on one server
    connection and the next statement on another — tested, it excluded
    nothing. An open transaction pins one server connection until commit, and
    the lock dies with it (commit, rollback or a dropped connection).
    ponytail: holds one pool connection + an idle transaction per running job;
    a lease row with an expiry is the upgrade if long jobs ever hurt vacuum.
    """
    async with _get_pool().acquire() as conn:
        async with conn.transaction():
            # The lock's transaction sits idle for the whole job, and Neon's
            # idle_in_transaction_session_timeout (5min) would kill it — and
            # the lock — halfway through a long drain. Off for this one only.
            await conn.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
            yield await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", key)


async def locked_send_due(*, settings, **scope) -> dict:
    """send_due under the drain lock. Every caller — the drain job, the tick,
    the inline win-back — goes through here: two concurrent drains would each
    compute the daily cap and the per-customer cooldown from the same stale
    read, and each pace at the full rate against Meta's app-level limit.
    Busy means another drain is running; the rows stay queued for the next."""
    from booking_engine.services.messaging.whatsapp_send import send_due
    async with singleton(DRAIN_LOCK) as got:
        if not got:
            return {"sent": 0, "busy": 1}
        return await send_due(settings=settings, **scope)


def seconds_to_next(interval: int, now: float | None = None) -> float:
    """Seconds until the next wall-clock multiple of `interval`."""
    now = time.time() if now is None else now
    return interval - (now % interval) or interval


async def run_every(
    name: str, interval: int, key: int, job: Callable[[], Awaitable[object]],
) -> None:
    """Forever: wait for the next aligned slot, run `job` if we get the lock.
    A failed run is logged, never fatal — the next slot retries."""
    while True:
        await asyncio.sleep(seconds_to_next(interval))
        try:
            async with singleton(key) as got:
                if got:
                    result = await job()
                    logger.info("scheduler.%s %s", name, result)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler.%s failed", name)


def start(settings) -> list[asyncio.Task]:
    """Start every job whose interval is set (> 0). Called from the lifespan."""
    # Imported here: the tick route pulls in most of the app.
    from booking_engine.api.routes.messaging_tick import run_tick
    from booking_engine.services.forwarding_heartbeat import emit_heartbeat_alerts
    from booking_engine.services.messaging.whatsapp_send import send_due

    jobs = [
        ("whatsapp_drain", settings.whatsapp_send_loop_seconds, DRAIN_LOCK,
         lambda: send_due(settings=settings)),
        ("messaging_tick", settings.messaging_tick_seconds, TICK_LOCK,
         lambda: run_tick(settings)),
        # Daily, not hourly: it pushes an alert per silent shop with no dedupe.
        ("forwarding_heartbeat", settings.forwarding_heartbeat_seconds, HEARTBEAT_LOCK,
         lambda: emit_heartbeat_alerts()),
    ]
    return [
        asyncio.create_task(run_every(name, interval, key, job))
        for name, interval, key, job in jobs if interval > 0
    ]
