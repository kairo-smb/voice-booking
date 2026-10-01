import asyncpg
import pytest

from booking_engine.db import connection


class _Conn:
    def __init__(self, fails):
        self.fails = fails

    async def fetch(self, sql, *args):
        if self.fails:
            self.fails -= 1
            raise asyncpg.exceptions.InvalidCachedStatementError("cached plan must not change result type")
        return [{"a": 1}]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_retries_once_on_stale_plan(monkeypatch):
    monkeypatch.setattr(connection, "_pool", _Pool(_Conn(1)))
    assert await connection.execute("select 1") == [{"a": 1}]


@pytest.mark.asyncio
async def test_second_failure_propagates(monkeypatch):
    monkeypatch.setattr(connection, "_pool", _Pool(_Conn(2)))
    with pytest.raises(asyncpg.exceptions.InvalidCachedStatementError):
        await connection.execute("select 1")
