import asyncio
import time

import pytest

from booking_engine.services.messaging.pacer import Pacer


@pytest.mark.asyncio
async def test_pacer_spaces_calls_out():
    """Three calls at 1200/min must take at least two gaps, not zero."""
    pacer = Pacer(per_minute=1200)          # 50ms apart
    start = time.monotonic()
    for _ in range(3):
        await pacer.wait()
    assert time.monotonic() - start >= 0.09


@pytest.mark.asyncio
async def test_pacer_does_not_delay_the_first_call():
    pacer = Pacer(per_minute=60)             # 1s apart
    start = time.monotonic()
    await pacer.wait()
    assert time.monotonic() - start < 0.05


@pytest.mark.asyncio
async def test_pacer_does_not_bank_credit_while_idle():
    """A long pause must not let the next batch burst.

    Without the `max(now, ...)` re-anchor, an idle minute would leave `_next`
    far in the past and the following claims would all fire at once — exactly
    the burst the pacer exists to prevent.
    """
    pacer = Pacer(per_minute=600)            # 100ms apart
    await pacer.wait()
    await asyncio.sleep(0.3)                 # idle, three gaps' worth
    start = time.monotonic()
    await pacer.wait()                       # this one is free (re-anchored)
    await pacer.wait()                       # this one must still wait a gap
    assert time.monotonic() - start >= 0.09


@pytest.mark.asyncio
async def test_a_non_positive_rate_disables_pacing_instead_of_crashing():
    """A misconfigured env var should slow nothing down, not divide by zero."""
    pacer = Pacer(per_minute=0)
    start = time.monotonic()
    for _ in range(5):
        await pacer.wait()
    assert time.monotonic() - start < 0.05
