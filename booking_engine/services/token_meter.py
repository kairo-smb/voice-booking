"""Token meter — warning tiers, detach decision, voice call debit."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal
from uuid import UUID

from booking_engine.db.token_basket_queries import (
    get_balance,
    get_last_refill_amount,
    insert_debit_event,
)


WarningTier = Literal["low_30pct", "critical_10pct", "below_reserve"]


class DetachReason(str, Enum):
    DISABLED = "disabled"
    BASKET_LOW = "basket_low"


@dataclass
class SessionDecision:
    attach: bool
    balance: int
    detach_reason: DetachReason | None


def compute_warning_tier(
    *, balance: int, last_refill: int, min_reserve: int = 1500
) -> WarningTier | None:
    """Return the current warning tier, or None if balance is healthy."""
    if balance < min_reserve:
        return "below_reserve"
    if last_refill <= 0:
        return None
    pct = balance / last_refill
    if pct <= 0.10:
        return "critical_10pct"
    if pct <= 0.30:
        return "low_30pct"
    return None


async def decide_session(
    *, shop_id: UUID, enabled: bool, min_reserve: int = 1500
) -> SessionDecision:
    """Decide whether to attach the AI for a new inbound call."""
    balance = await get_balance(shop_id)
    if not enabled:
        return SessionDecision(attach=False, balance=balance,
                               detach_reason=DetachReason.DISABLED)
    if balance < min_reserve:
        return SessionDecision(attach=False, balance=balance,
                               detach_reason=DetachReason.BASKET_LOW)
    return SessionDecision(attach=True, balance=balance, detach_reason=None)


async def record_voice_debit(
    *,
    shop_id: UUID,
    call_id: UUID,
    duration_seconds: int,
    tool_token_cost: int,
    tokens_per_second: int,
    previous_tier: WarningTier | None = None,
) -> None:
    """Debit a completed call's tokens from the shop basket."""
    tokens = duration_seconds * tokens_per_second + tool_token_cost
    await insert_debit_event(
        shop_id=shop_id,
        tokens=tokens,
        source="voice_call",
        voice_call_id=call_id,
    )
    # After debit, check whether we crossed a warning threshold
    from booking_engine.services.balance_alerts import maybe_emit_balance_alert

    balance = await get_balance(shop_id)
    last_refill = await get_last_refill_amount(shop_id)
    await maybe_emit_balance_alert(
        shop_id=shop_id, balance=balance, last_refill=last_refill,
        previous_tier=previous_tier,
    )
