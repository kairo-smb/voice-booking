"""DB access for ai_token_baskets and ai_token_basket_events."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute_one, execute_void


async def get_balance(shop_id: UUID) -> int:
    """Return current basket balance for the shop (0 if no basket)."""
    row = await execute_one(
        "SELECT balance_tokens FROM business_app_core.ai_token_baskets "
        "WHERE shop_id = $1 LIMIT 1",
        shop_id,
    )
    return int(row["balance_tokens"]) if row else 0


async def get_last_refill_amount(shop_id: UUID) -> int:
    """Return the most recent positive basket-credit amount (for % thresholds)."""
    row = await execute_one(
        """
        SELECT tokens FROM business_app_core.ai_token_basket_events
        WHERE shop_id = $1 AND tokens > 0
        ORDER BY created_at DESC LIMIT 1
        """,
        shop_id,
    )
    return int(row["tokens"]) if row else 0


async def insert_debit_event(
    *,
    shop_id: UUID,
    tokens: int,
    source: str,
    voice_call_id: UUID | None,
) -> None:
    await execute_void(
        """
        INSERT INTO business_app_core.ai_token_basket_events
        (shop_id, tokens, source, voice_call_id, created_at)
        VALUES ($1, $2, $3, $4, now())
        """,
        shop_id, -abs(tokens), source, voice_call_id,
    )
    await execute_void(
        """
        UPDATE business_app_core.ai_token_baskets
        SET balance_tokens = balance_tokens - $2,
            updated_at = now()
        WHERE shop_id = $1
        """,
        shop_id, abs(tokens),
    )
