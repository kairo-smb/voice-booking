"""Token-basket access for the voice/SMS spend path.

The basket (`business_app_core.ai_token_basket`) and its ledger
(`ai_run_ledger`) are owned by the webapp. This repo only READS the balance
here and charges over HTTP (`booking_engine/clients/webapp_credits.py`).
There is no local deduction arithmetic any more — the old `insert_debit_event`
(a second, independent granted-first deduction whose only record was the
webapp's legacy spend-log table, now being dropped) is gone. A charge POSTs a
pre-converted credits amount to the webapp's charge-actual endpoint, which
performs the single locked deduction and writes the ledger row.
"""
from __future__ import annotations

from uuid import UUID

from booking_engine.clients import webapp_credits
from booking_engine.config import Settings
from booking_engine.db.connection import execute_one


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


async def try_debit_for_message(
    *,
    shop_id: UUID,
    credits: int,
    sms_message_id: UUID | None = None,
    settings: Settings,
) -> bool:
    """Charge for an outbound SMS, or refuse. Returns False without charging.

    Unlike the voice path this never bills a message the webapp refused (an
    empty basket): the charge runs as a single locked transaction on the
    webapp side (`deductCredits`), so two concurrent sends cannot overdraw by
    one message — a message that can't be billed is simply not sent.
    """
    if credits <= 0:
        return True   # a free message writes no ledger row
    return await webapp_credits.charge_actual(
        shop_id=shop_id,
        run_type=webapp_credits.SMS_SEND,
        run_ref=str(sms_message_id) if sms_message_id is not None else None,
        credits=credits,
        settings=settings,
    )
