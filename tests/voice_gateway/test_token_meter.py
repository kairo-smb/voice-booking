"""Tests for token meter — debit, warning tiers, and detach decision."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.services.token_meter import (
    DetachReason,
    SessionDecision,
    compute_warning_tier,
    decide_session,
    record_voice_debit,
)


def test_compute_warning_tier_normal():
    assert compute_warning_tier(balance=5000, last_refill=10000) is None


def test_compute_warning_tier_30pct():
    # 3000 / 10000 == 30%; threshold inclusive
    assert compute_warning_tier(balance=3000, last_refill=10000) == "low_30pct"


def test_compute_warning_tier_10pct():
    # balance=1800 is above min_reserve(1500) but 9% of 20000 → critical_10pct
    assert compute_warning_tier(balance=1800, last_refill=20000, min_reserve=1500) == "critical_10pct"


def test_compute_warning_tier_below_reserve():
    assert compute_warning_tier(balance=500, last_refill=10000) == "below_reserve"


@pytest.mark.asyncio
async def test_decide_session_attaches_when_balance_ok():
    with patch("booking_engine.services.token_meter.get_balance",
               new=AsyncMock(return_value=5000)):
        decision = await decide_session(shop_id=uuid4(), enabled=True,
                                        min_reserve=1500)
        assert decision.attach is True
        assert decision.detach_reason is None


@pytest.mark.asyncio
async def test_decide_session_detaches_when_disabled():
    with patch("booking_engine.services.token_meter.get_balance",
               new=AsyncMock(return_value=5000)):
        decision = await decide_session(shop_id=uuid4(), enabled=False,
                                        min_reserve=1500)
        assert decision.attach is False
        assert decision.detach_reason == DetachReason.DISABLED


@pytest.mark.asyncio
async def test_decide_session_detaches_when_below_reserve():
    with patch("booking_engine.services.token_meter.get_balance",
               new=AsyncMock(return_value=500)):
        decision = await decide_session(shop_id=uuid4(), enabled=True,
                                        min_reserve=1500)
        assert decision.attach is False
        assert decision.detach_reason == DetachReason.BASKET_LOW


@pytest.mark.asyncio
async def test_record_voice_debit_writes_event():
    call_id = uuid4()
    shop_id = uuid4()
    with patch("booking_engine.services.token_meter.insert_debit_event",
               new=AsyncMock(return_value=None)) as ins, \
         patch("booking_engine.services.token_meter.get_balance",
               new=AsyncMock(return_value=5000)), \
         patch("booking_engine.services.token_meter.get_last_refill_amount",
               new=AsyncMock(return_value=10000)), \
         patch("booking_engine.services.balance_alerts.maybe_emit_balance_alert",
               new=AsyncMock(return_value=None)):
        await record_voice_debit(
            shop_id=shop_id, call_id=call_id,
            duration_seconds=180, tool_token_cost=200,
            tokens_per_second=18,
        )
        ins.assert_awaited_once()
        kwargs = ins.await_args.kwargs
        # 180 * 18 + 200 = 3440
        assert kwargs["tokens"] == 3440
        assert kwargs["source"] == "voice_call"
        assert kwargs["voice_call_id"] == call_id
