"""Tests for the voice gateway DB pool helpers."""
from __future__ import annotations

import pytest

from voice_gateway.db import execute


@pytest.mark.asyncio
async def test_pool_raises_when_not_initialized(monkeypatch):
    import voice_gateway.db as db_mod
    monkeypatch.setattr(db_mod, "_pool", None, raising=False)
    with pytest.raises(RuntimeError):
        await execute("SELECT 1")
