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


async def is_opted_out(shop_id: UUID, phone_normalized: str) -> bool:
    row = await execute_one(
        "SELECT 1 AS hit FROM sms.opt_outs WHERE shop_id = $1 AND phone_normalized = $2",
        shop_id, phone_normalized,
    )
    return row is not None


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


async def record_opt_out(
    *, shop_id: UUID, phone_normalized: str, keyword: str, raw_body: str
) -> None:
    """Suppression list entry. Idempotent — a second STOP is not an error."""
    await execute_void(
        """
        INSERT INTO sms.opt_outs (shop_id, phone_normalized, keyword, raw_body)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (shop_id, phone_normalized) DO NOTHING
        """,
        shop_id, phone_normalized, keyword, raw_body,
    )


async def withdraw_marketing_consent(*, shop_id: UUID, phone_normalized: str) -> int:
    """Reflect the STOP in the single source of truth.

    Without this the webapp keeps listing the customer as consenting while SMS
    silently suppresses them. Returns the number of rows updated (0 when the
    phone matches no customer — the opt_outs row still stands alone).
    """
    row = await execute_one(
        """
        WITH updated AS (
            UPDATE business_app_core.customers
            SET marketing_consent = false,
                marketing_consent_withdrawn_at = now(),
                marketing_consent_source = 'sms_stop',
                updated_at = now()
            WHERE shop_id = $1 AND phone_normalized = $2
              AND marketing_consent_withdrawn_at IS NULL
            RETURNING 1
        )
        SELECT count(*) AS n FROM updated
        """,
        shop_id, phone_normalized,
    )
    return int(row["n"]) if row else 0


async def get_shop_by_sender_number(number: str) -> UUID | None:
    """Inbound webhooks identify the shop by the number that was texted."""
    row = await execute_one(
        "SELECT shop_id FROM voice_agent.shop_telephony WHERE kairo_number = $1",
        number,
    )
    return row["shop_id"] if row else None
