"""SQL for the `sms` schema. See docs/messaging-design.md §4.1."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute_one, execute_void


async def get_shop_sender_number(shop_id: UUID) -> str | None:
    """The shop's own Twilio DID — the same number that answers voice calls."""
    row = await execute_one(
        "SELECT kairo_number FROM voice_agent.shop_telephony WHERE shop_id = $1",
        shop_id,
    )
    return row["kairo_number"] if row else None


async def get_customer_for_send(shop_id: UUID, customer_id: UUID) -> dict | None:
    """Consent + phone for one customer, scoped to the shop (never cross-shop)."""
    return await execute_one(
        """
        SELECT id, full_name, phone, phone_normalized,
               marketing_consent, marketing_consent_granted_at,
               marketing_consent_withdrawn_at
        FROM business_app_core.customers
        WHERE id = $1 AND shop_id = $2
        """,
        customer_id, shop_id,
    )


async def insert_outbound(
    *,
    shop_id: UUID,
    customer_id: UUID | None,
    to_phone: str,
    from_number: str,
    body: str,
    segments: int,
    encoding: str,
    status: str,
    suppressed_reason: str | None = None,
) -> UUID:
    row = await execute_one(
        """
        INSERT INTO sms.outbound_messages
            (shop_id, customer_id, to_phone, from_number, body,
             segments, encoding, status, suppressed_reason)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        RETURNING id
        """,
        shop_id, customer_id, to_phone, from_number, body,
        segments, encoding, status, suppressed_reason,
    )
    return row["id"]


async def mark_sent(
    *, message_id: UUID, provider_sid: str, price_usd: float | None, credits: int
) -> None:
    await execute_void(
        """
        UPDATE sms.outbound_messages
        SET status = 'sent', provider_sid = $2, price_usd = $3,
            credits_charged = $4, sent_at = now(), updated_at = now()
        WHERE id = $1
        """,
        message_id, provider_sid, price_usd, credits,
    )


async def mark_failed(*, message_id: UUID, error_code: str) -> None:
    await execute_void(
        """
        UPDATE sms.outbound_messages
        SET status = 'failed', error_code = $2, updated_at = now()
        WHERE id = $1
        """,
        message_id, error_code,
    )


async def update_status_by_sid(
    *, provider_sid: str, status: str, price_usd: float | None, error_code: str | None
) -> None:
    """Twilio status callback. Only advances to terminal states we recognise."""
    await execute_void(
        """
        UPDATE sms.outbound_messages
        SET status = $2,
            price_usd = COALESCE($3, price_usd),
            error_code = COALESCE($4, error_code),
            updated_at = now()
        WHERE provider_sid = $1
        """,
        provider_sid, status, price_usd, error_code,
    )
