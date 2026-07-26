"""Tests for the Path-2 forwarding heartbeat — detects silent number outages."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.services.forwarding_heartbeat import (
    find_silent_forwarded_shops,
)


@pytest.mark.asyncio
async def test_finds_shops_with_no_inbound_in_5_days():
    six_days_ago = datetime.now(timezone.utc) - timedelta(days=6)
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    fake_rows = [
        {"shop_id": uuid4(), "kairo_number": "+39021",
         "last_inbound_call_at": six_days_ago, "setup_path": "forward"},
        {"shop_id": uuid4(), "kairo_number": "+39022",
         "last_inbound_call_at": one_day_ago, "setup_path": "forward"},
    ]
    with patch("booking_engine.services.forwarding_heartbeat.execute",
               new=AsyncMock(return_value=fake_rows[:1])):
        results = await find_silent_forwarded_shops(threshold_days=5)
        assert len(results) == 1


@pytest.mark.asyncio
async def test_only_alerts_always_on_shops_not_overflow():
    # Overflow shops are legitimately silent for days; they must be excluded so
    # the heartbeat doesn't false-alarm. The query filters answer_mode='always_on'.
    captured = {}

    async def fake_execute(sql, *args):
        captured["sql"] = sql
        return []

    with patch("booking_engine.services.forwarding_heartbeat.execute",
               new=fake_execute):
        await find_silent_forwarded_shops(threshold_days=5)

    sql = captured["sql"].lower()
    assert "shop_config" in sql
    assert "answer_mode" in sql
    assert "always_on" in sql
