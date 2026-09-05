"""Space outbound provider calls so a tick can't burst.

The limit that can actually be tripped is not Meta's per-number ceiling — 20
messages/second under coexistence, far above anything one salon does — but the
Graph API's **app-level** limit, which every tenant shares. One tick claims up
to MAX_PER_TICK rows across every shop at once, so without this the whole
batch goes out as fast as the loop runs.

ponytail: a monotonic next-slot stamp, not a token bucket. Dispatch is serial
(the send loop mutates per-shop remaining-cap counters as it goes), so there is
never more than one waiter and a bucket's burst allowance would be unused
machinery. If sends ever go concurrent, this becomes a semaphore + bucket.
"""
from __future__ import annotations

import asyncio
import time


class Pacer:
    """Allow at most `per_minute` calls, evenly spaced."""

    def __init__(self, per_minute: int):
        # A non-positive rate means "no pacing" rather than a division error:
        # a misconfigured env var should slow nothing down, not crash the tick.
        self._gap = 60.0 / per_minute if per_minute > 0 else 0.0
        self._next = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        if now < self._next:
            await asyncio.sleep(self._next - now)
        # `max` re-anchors after an idle gap, so a long pause doesn't bank up
        # credit and let the next batch burst.
        self._next = max(now, self._next) + self._gap
