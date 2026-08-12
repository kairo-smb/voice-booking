"""DB access for the webapp's real ai_token_basket / ai_token_log tables.

Schema (production, owned by webapp):
  business_app_core.ai_token_basket(shop_id PK, granted_credits, purchased_credits,
                                     granted_expires_at, updated_at)
  business_app_core.ai_token_log(id, shop_id, payment_id, credits_used,
                                  source ai_credit_source, created_at,
                                  voice_call_id)   -- voice_call_id added by Plan A

`balance` here = effective granted (zero if expired) + purchased. Debits use
granted-first, falling back to purchased — mirrors the webapp's deductCredits().
"""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute_one, execute_void


async def get_balance(shop_id: UUID) -> int:
    """Return current effective basket balance (granted + purchased) for the shop."""
    row = await execute_one(
        """
        SELECT granted_credits, purchased_credits, granted_expires_at
        FROM business_app_core.ai_token_basket
        WHERE shop_id = $1
        """,
        shop_id,
    )
    if not row:
        return 0
    granted = int(row["granted_credits"])
    if row["granted_expires_at"] is not None:
        # When the grant has expired, treat granted as zero. We compare with `now()`
        # in SQL on the next debit call; for read-only balance display we compare
        # in Python for symmetry with the webapp's deductCredits behavior.
        from datetime import datetime, timezone
        if row["granted_expires_at"] < datetime.now(timezone.utc):
            granted = 0
    return granted + int(row["purchased_credits"])


async def get_last_refill_amount(shop_id: UUID) -> int:
    """Approximate "last refill" for percentage-tier alerts.

    The real schema has no canonical "refill" event row — top-ups update
    `purchased_credits` directly. As a pragmatic v1, we use the current total
    capacity (granted + purchased) as the denominator. Percentage tiers then
    reflect "fraction of current basket left" rather than "fraction of last
    top-up left", which is close enough to drive the merchant warning banners.

    A future iteration should join `payments` to find the most recent credit
    purchase amount.
    """
    return await get_balance(shop_id)


async def insert_debit_event(
    *,
    shop_id: UUID,
    tokens: int,
    source: str,  # kept for signature compatibility; we map to 'granted'/'purchased'
    voice_call_id: UUID | None,
    sms_message_id: UUID | None = None,
    whatsapp_message_id: UUID | None = None,
) -> None:
    """Deduct `tokens` from the basket using granted-first ordering, log to ai_token_log."""
    amount = abs(tokens)
    row = await execute_one(
        """
        SELECT granted_credits, purchased_credits, granted_expires_at
        FROM business_app_core.ai_token_basket
        WHERE shop_id = $1
        FOR UPDATE
        """,
        shop_id,
    )
    if not row:
        return

    from datetime import datetime, timezone
    expired = (
        row["granted_expires_at"] is not None
        and row["granted_expires_at"] < datetime.now(timezone.utc)
    )
    effective_granted = 0 if expired else int(row["granted_credits"])
    purchased = int(row["purchased_credits"])

    debit_source: str
    if effective_granted >= amount:
        debit_source = "granted"
        await execute_void(
            """
            UPDATE business_app_core.ai_token_basket
            SET granted_credits = granted_credits - $2, updated_at = now()
            WHERE shop_id = $1
            """,
            shop_id, amount,
        )
    elif purchased >= amount:
        debit_source = "purchased"
        await execute_void(
            """
            UPDATE business_app_core.ai_token_basket
            SET purchased_credits = purchased_credits - $2, updated_at = now()
            WHERE shop_id = $1
            """,
            shop_id, amount,
        )
    else:
        # Insufficient credits: drain whichever bucket has the most. The TwiML
        # detach matrix has already gated on min_reserve, so this path is only
        # reached for in-call overages.
        if effective_granted >= purchased:
            debit_source = "granted"
            await execute_void(
                "UPDATE business_app_core.ai_token_basket "
                "SET granted_credits = 0, updated_at = now() WHERE shop_id = $1",
                shop_id,
            )
        else:
            debit_source = "purchased"
            await execute_void(
                "UPDATE business_app_core.ai_token_basket "
                "SET purchased_credits = 0, updated_at = now() WHERE shop_id = $1",
                shop_id,
            )

    await execute_void(
        """
        INSERT INTO business_app_core.ai_token_log
            (shop_id, credits_used, source, voice_call_id,
             sms_message_id, whatsapp_message_id, created_at)
        VALUES ($1, $2, $3::ai_credit_source, $4, $5, $6, now())
        """,
        shop_id, amount, debit_source, voice_call_id,
        sms_message_id, whatsapp_message_id,
    )


async def try_debit_for_message(
    *,
    shop_id: UUID,
    credits: int,
    sms_message_id: UUID | None = None,
    whatsapp_message_id: UUID | None = None,
) -> bool:
    """Debit for an outbound message, or refuse. Returns False without debiting.

    Unlike insert_debit_event (the voice path) this never overdraws: a live call
    can't be un-answered, but a message can simply not be sent. See
    docs/messaging-design.md §5.2.
    """
    if credits <= 0:
        return True   # a free message writes no ledger row
    # ponytail: check-then-debit, not one locked transaction. Two concurrent
    # sends could overdraw by one message; sends are owner-triggered and
    # effectively serial today. Wrap both in a single FOR UPDATE tx if bulk
    # campaigns ever run concurrently.
    if await get_balance(shop_id) < credits:
        return False
    await insert_debit_event(
        shop_id=shop_id,
        tokens=credits,
        source="granted",
        voice_call_id=None,
        sms_message_id=sms_message_id,
        whatsapp_message_id=whatsapp_message_id,
    )
    return True
