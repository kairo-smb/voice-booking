"""Unit tests for asyncpg-based connection module."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from booking_engine.config import Settings


@pytest.fixture
def settings():
    return Settings(database_url="postgresql://user:pass@localhost:5432/testdb")


def _make_pool_with_conn(mock_conn):
    """Create a mock pool whose acquire() returns an async context manager yielding mock_conn."""
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_cm
    mock_pool.close = AsyncMock()
    return mock_pool


class TestInitConnection:
    @patch("booking_engine.db.connection.asyncpg")
    async def test_creates_pool(self, mock_asyncpg, settings):
        mock_pool = AsyncMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        from booking_engine.db.connection import init_connection
        await init_connection(settings)

        mock_asyncpg.create_pool.assert_called_once_with(
            dsn=settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
        )


class TestCloseConnection:
    async def test_closes_pool(self):
        import booking_engine.db.connection as mod
        mock_pool = AsyncMock()
        mod._pool = mock_pool
        await mod.close_connection()
        mock_pool.close.assert_called_once()
        assert mod._pool is None


class TestGetPool:
    def test_raises_if_not_initialized(self):
        import booking_engine.db.connection as mod
        mod._pool = None
        with pytest.raises(RuntimeError, match="not initialized"):
            mod._get_pool()


class TestExecute:
    async def test_returns_list_of_dicts(self):
        import booking_engine.db.connection as mod
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[{"id": "abc", "name": "test"}])

        mod._pool = _make_pool_with_conn(mock_conn)

        result = await mod.execute("SELECT * FROM shops WHERE id = $1", "abc")
        assert isinstance(result, list)
        assert result[0] == {"id": "abc", "name": "test"}
        mock_conn.fetch.assert_called_once_with("SELECT * FROM shops WHERE id = $1", "abc")


class TestExecuteOne:
    async def test_returns_none_when_no_row(self):
        import booking_engine.db.connection as mod
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)

        mod._pool = _make_pool_with_conn(mock_conn)

        result = await mod.execute_one("SELECT * FROM shops WHERE id = $1", "abc")
        assert result is None

    async def test_returns_dict_when_row_found(self):
        import booking_engine.db.connection as mod
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": "abc", "name": "test"})

        mod._pool = _make_pool_with_conn(mock_conn)

        result = await mod.execute_one("SELECT * FROM shops WHERE id = $1", "abc")
        assert result == {"id": "abc", "name": "test"}


class TestExecuteVoid:
    async def test_executes_without_return(self):
        import booking_engine.db.connection as mod
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 1")

        mod._pool = _make_pool_with_conn(mock_conn)

        await mod.execute_void("INSERT INTO shops (id) VALUES ($1)", "abc")
        mock_conn.execute.assert_called_once_with("INSERT INTO shops (id) VALUES ($1)", "abc")
