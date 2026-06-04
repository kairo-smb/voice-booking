"""/voice/balance/{shop_id} — exposes current balance + warning tier for webapp banners."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from booking_engine.api.deps import require_control_plane_token
from booking_engine.db.token_basket_queries import get_balance, get_last_refill_amount
from booking_engine.services.token_meter import compute_warning_tier

router = APIRouter(prefix="/voice/balance", tags=["voice-balance"])


@router.get("/{shop_id}")
async def status(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    balance = await get_balance(shop_id)
    last_refill = await get_last_refill_amount(shop_id)
    tier = compute_warning_tier(balance=balance, last_refill=last_refill)
    return {"data": {"balance": balance, "last_refill": last_refill, "tier": tier}}
