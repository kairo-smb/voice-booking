"""Balance alert emitter — fires push events on warning tier transitions."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from booking_engine.clients.push_notifications import send_push
from booking_engine.services.token_meter import compute_warning_tier


_EVENT_FOR_TIER = {
    "low_30pct": "voice_balance_low_30pct",
    "critical_10pct": "voice_balance_critical_10pct",
    "below_reserve": "voice_detached",
}


async def maybe_emit_balance_alert(
    *,
    shop_id: UUID,
    balance: int,
    last_refill: int,
    previous_tier: Literal["low_30pct", "critical_10pct", "below_reserve"] | None,
) -> None:
    """Emit a push event if the warning tier changed to a more severe level."""
    new_tier = compute_warning_tier(balance=balance, last_refill=last_refill)
    if new_tier is None or new_tier == previous_tier:
        return
    # Only emit when crossing to a more severe tier
    severity = {"low_30pct": 1, "critical_10pct": 2, "below_reserve": 3}
    prev_score = severity.get(previous_tier or "", 0)
    if severity[new_tier] <= prev_score:
        return
    await send_push(
        shop_id=shop_id,
        event=_EVENT_FOR_TIER[new_tier],
        payload={"balance": balance, "last_refill": last_refill},
    )
