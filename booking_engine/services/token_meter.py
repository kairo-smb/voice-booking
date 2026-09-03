"""Token meter — warning tiers, detach decision, voice call charge."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Literal
from uuid import UUID

from booking_engine.clients import webapp_credits
from booking_engine.config import Settings
from booking_engine.db.token_basket_queries import (
    get_balance,
    get_last_refill_amount,
)

logger = logging.getLogger(__name__)


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
    settings: Settings,
    previous_tier: WarningTier | None = None,
) -> None:
    """Bill a completed call. The basket deduction is the webapp's, over HTTP.

    `tokens` (seconds × rate + tool cost) is the credit amount this call
    costs — the meter's own number, so it is POSTed as pre-converted credits,
    never USD. If the webapp refuses the charge (402, basket emptied) the call
    has already happened and can't be un-answered: the client logs it loudly
    and the basket is left exactly as the webapp's locked transaction left it.
    We no longer drain the bucket to an arbitrary value on overage.
    """
    tokens = duration_seconds * tokens_per_second + tool_token_cost
    if tokens > 0:
        charged = await webapp_credits.charge_actual(
            shop_id=shop_id,
            run_type=webapp_credits.VOICE_CALL,
            run_ref=str(call_id),
            credits=tokens,
            settings=settings,
        )
        if not charged:
            logger.warning(
                "voice_call.unbilled shop=%s call=%s credits=%s — charge refused",
                shop_id, call_id, tokens,
            )
    # After the charge, check whether we crossed a warning threshold
    from booking_engine.services.balance_alerts import maybe_emit_balance_alert

    balance = await get_balance(shop_id)
    last_refill = await get_last_refill_amount(shop_id)
    await maybe_emit_balance_alert(
        shop_id=shop_id, balance=balance, last_refill=last_refill,
        previous_tier=previous_tier,
    )
