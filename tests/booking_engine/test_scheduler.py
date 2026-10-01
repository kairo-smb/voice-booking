"""The in-process scheduler: each job is a fleet-wide singleton."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from booking_engine.services import scheduler


def test_jobs_align_to_the_wall_clock():
    # Every machine computes the same slot, so they all try at once and all
    # but one skip — not N runs per interval from drifting timers.
    assert scheduler.seconds_to_next(3600, now=7200 + 600) == 3000
    assert scheduler.seconds_to_next(60, now=120) == 60


def _patch(monkeypatch, got: bool):
    @asynccontextmanager
    async def _lock(key):
        yield got
    monkeypatch.setattr(scheduler, "singleton", _lock)
    monkeypatch.setattr(scheduler, "seconds_to_next", lambda interval: 0)


@pytest.mark.asyncio
async def test_run_every_survives_a_failed_run_and_skips_when_locked_elsewhere(monkeypatch):
    calls = []

    async def job():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db blip")   # must not kill the loop
        if len(calls) == 3:
            raise asyncio.CancelledError

    _patch(monkeypatch, got=True)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_every("t", 60, 1, job)
    assert len(calls) == 3

    # Another machine holds the lock: the job never runs here.
    calls.clear()
    _patch(monkeypatch, got=False)
    task = asyncio.create_task(scheduler.run_every("t", 60, 1, job))
    await asyncio.sleep(0.01)
    task.cancel()
    assert calls == []


@pytest.mark.asyncio
async def test_locked_send_due_skips_while_another_drain_runs(monkeypatch):
    _patch(monkeypatch, got=False)
    assert await scheduler.locked_send_due(settings=None) == {"sent": 0, "busy": 1}
