"""Six months, and the shape of the sweep that enforces it.

The boundary is pinned here rather than left to read off the implementation:
a row that has reached exactly RETENTION **is expired**. Erring toward
deletion is the direction data minimisation wants, and it is the opposite of
`window_open`'s strictness for the opposite reason — there, being a hair late
costs a Meta error; here, being a hair late costs personal data kept past its
policy.

The structural assertions about the statement (one statement, both directions,
`voice_agent.calls` untouched) are here because they are properties of the SQL
text, not of any particular row. The behavioural proof — that a real batch
really deletes both halves, is really bounded, and that a second run really
removes nothing — was run against a scratch Postgres with real rows; see the
commit message.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import wa_retention

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------- the rule

def test_two_hundred_days_old_is_expired():
    assert wa_retention.is_expired(at=NOW - timedelta(days=200), now=NOW) is True


def test_one_hundred_days_old_is_not():
    assert wa_retention.is_expired(at=NOW - timedelta(days=100), now=NOW) is False


def test_exactly_at_the_boundary_is_expired():
    """Pinned deliberately: at exactly 183 days the row goes.

    A row that has reached six months has *had* its six months, and between the
    two possible off-by-ones the one that deletes is the one a retention policy
    is for.
    """
    assert wa_retention.RETENTION == timedelta(days=183)
    assert wa_retention.is_expired(at=NOW - wa_retention.RETENTION, now=NOW) is True
    just_inside = NOW - wa_retention.RETENTION + timedelta(seconds=1)
    assert wa_retention.is_expired(at=just_inside, now=NOW) is False


def test_is_expired_is_pure(monkeypatch):
    """A timestamp in, a bool out — `now` is an argument, and no database."""
    from booking_engine.db import connection

    def boom(*a, **kw):  # pragma: no cover - must never be called
        raise AssertionError("is_expired touched the database")

    monkeypatch.setattr(connection, "execute", boom)
    monkeypatch.setattr(connection, "execute_one", boom)

    at = NOW - timedelta(days=184)
    assert wa_retention.is_expired(at=at, now=NOW) is True
    assert wa_retention.is_expired(at=at, now=NOW - timedelta(days=10)) is False


# -------------------------------------------------------------- the statement

def test_the_sweep_deletes_both_directions_in_one_statement():
    """Half a conversation is still personal data and no longer readable as a
    conversation, so the two deletes share a statement and a transaction."""
    sql = wq._PURGE
    assert "DELETE FROM whatsapp.inbound_messages" in sql
    assert "DELETE FROM whatsapp.outbound_messages" in sql
    # One statement: CTEs, not two calls. A semicolon would mean two
    # transactions, and a crash between them is exactly the halved conversation.
    assert ";" not in sql


def test_the_sweep_leaves_voice_agent_calls_alone():
    """The session row is the business record of an appointment being made. It
    outlives the chat that produced it and is not this policy's to expire."""
    assert "voice_agent" not in wq._PURGE


def test_the_batch_is_counted_in_threads_not_rows():
    """The unit that must not be split is the conversation, so the LIMIT sits
    on the thread list and both deletes follow it."""
    sql = wq._PURGE
    assert "LIMIT $2" in sql
    limit_at = sql.index("LIMIT $2")
    assert limit_at < sql.index("DELETE FROM whatsapp.inbound_messages")
    assert limit_at < sql.index("DELETE FROM whatsapp.outbound_messages")


# ----------------------------------------------------------------- the sweep

@pytest.fixture
def purged(monkeypatch):
    """Capture what the sweep asks the database for."""
    seen: list[dict] = []
    result = {"threads": 0, "inbound": 0, "outbound": 0}

    async def purge(*, cutoff, threads):
        seen.append({"cutoff": cutoff, "threads": threads})
        return dict(result)

    monkeypatch.setattr(wa_retention.wq, "purge_expired_threads", purge)
    return {"seen": seen, "result": result}


@pytest.mark.asyncio
async def test_the_sweep_cuts_at_now_minus_retention_and_is_bounded(purged):
    purged["result"].update({"threads": 3, "inbound": 12, "outbound": 9})
    counts = await wa_retention.sweep()

    assert counts == {"threads": 3, "inbound": 12, "outbound": 9, "errors": 0}
    assert purged["seen"][0]["threads"] == wa_retention.BATCH_THREADS
    age = datetime.now(timezone.utc) - purged["seen"][0]["cutoff"]
    assert abs(age - wa_retention.RETENTION) < timedelta(seconds=5)


@pytest.mark.asyncio
async def test_a_run_with_nothing_expired_reports_zero(purged):
    """Re-running immediately is a no-op: the predicate is a timestamp against
    rows that no longer exist, so the second run matches nothing."""
    counts = await wa_retention.sweep()
    assert counts == {"threads": 0, "inbound": 0, "outbound": 0, "errors": 0}


@pytest.mark.asyncio
async def test_a_failed_sweep_is_counted_not_raised(monkeypatch):
    """It is a tick stage: one bad run must be an error count, never a 500."""
    async def boom(*, cutoff, threads):
        raise RuntimeError("the database is having a day")

    monkeypatch.setattr(wa_retention.wq, "purge_expired_threads", boom)
    counts = await wa_retention.sweep()
    assert counts == {"threads": 0, "inbound": 0, "outbound": 0, "errors": 1}
