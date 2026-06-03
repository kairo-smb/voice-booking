"""Tests for the balance-alert emitter — sends push events on tier transitions."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.services.balance_alerts import maybe_emit_balance_alert


@pytest.mark.asyncio
async def test_emits_low_30pct_when_crossing_threshold():
    shop_id = uuid4()
    with patch("booking_engine.services.balance_alerts.send_push",
               new=AsyncMock(return_value=None)) as push:
        await maybe_emit_balance_alert(
            shop_id=shop_id, balance=2999, last_refill=10000,
            previous_tier=None,
        )
        push.assert_awaited_once()
        assert push.await_args.kwargs["event"] == "voice_balance_low_30pct"


@pytest.mark.asyncio
async def test_emits_critical_when_dropping_further():
    shop_id = uuid4()
    # balance=1800 is above min_reserve(1500) but 9% of 20000 → critical_10pct
    with patch("booking_engine.services.balance_alerts.send_push",
               new=AsyncMock(return_value=None)) as push:
        await maybe_emit_balance_alert(
            shop_id=shop_id, balance=1800, last_refill=20000,
            previous_tier="low_30pct",
        )
        push.assert_awaited_once()
        assert push.await_args.kwargs["event"] == "voice_balance_critical_10pct"


@pytest.mark.asyncio
async def test_emits_detached_when_below_reserve():
    shop_id = uuid4()
    with patch("booking_engine.services.balance_alerts.send_push",
               new=AsyncMock(return_value=None)) as push:
        await maybe_emit_balance_alert(
            shop_id=shop_id, balance=100, last_refill=10000,
            previous_tier="critical_10pct",
        )
        push.assert_awaited_once()
        assert push.await_args.kwargs["event"] == "voice_detached"


@pytest.mark.asyncio
async def test_no_emit_when_tier_unchanged():
    shop_id = uuid4()
    with patch("booking_engine.services.balance_alerts.send_push",
               new=AsyncMock(return_value=None)) as push:
        await maybe_emit_balance_alert(
            shop_id=shop_id, balance=2500, last_refill=10000,
            previous_tier="low_30pct",
        )
        push.assert_not_awaited()
