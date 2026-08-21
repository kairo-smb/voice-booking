"""SQL for the `whatsapp` schema. See booking_engine/db/sql/14_whatsapp_schema.sql.

Customer consent is read through `sms_queries.get_customer_for_send` — the
same single source of truth (`business_app_core.customers`) the SMS path uses.
There is deliberately no WhatsApp-specific consent table.
"""
from __future__ import annotations

import json
from uuid import UUID

from booking_engine.db.connection import execute, execute_one, execute_void


# --------------------------------------------------------------------- senders

async def get_sender(shop_id: UUID) -> dict | None:
    return await execute_one(
        "SELECT * FROM whatsapp.senders WHERE shop_id = $1", shop_id
    )


async def upsert_sender(
    *, shop_id: UUID, display_name: str, source: str, subaccount_sid: str | None = None
) -> dict:
    row = await execute_one(
        """
        INSERT INTO whatsapp.senders (shop_id, display_name, source, subaccount_sid)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (shop_id) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            source = EXCLUDED.source,
            subaccount_sid = COALESCE(whatsapp.senders.subaccount_sid,
                                      EXCLUDED.subaccount_sid),
            updated_at = now()
        RETURNING *
        """,
        shop_id, display_name, source, subaccount_sid,
    )
    return row  # type: ignore[return-value]


async def set_sender_fields(shop_id: UUID, **fields) -> None:
    """Persist whatever we just learned, one column at a time.

    Same shape as number_request_queries.set_sids: every Twilio SID is written
    the moment it exists, so a crash halfway through onboarding leaves a
    resumable row instead of an orphaned Twilio object nothing references.
    """
    allowed = {
        "status", "subaccount_sid", "subaccount_auth_token", "waba_id",
        "sender_sid", "phone_number", "quality_rating", "messaging_limit",
        "offline_reason", "daily_cap",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    verified = ", verified_at = now()" if fields.get("status") == "online" else ""
    await execute_void(
        f"UPDATE whatsapp.senders SET {sets}{verified}, updated_at = now() "
        f"WHERE shop_id = $1",
        shop_id, *fields.values(),
    )


async def list_verifying_senders() -> list[dict]:
    """Senders mid-verification — polled by the tick to pick up Meta's verdict."""
    return await execute(
        "SELECT * FROM whatsapp.senders WHERE status IN ('verifying','pending_signup') "
        "AND sender_sid IS NOT NULL"
    )


async def get_sender_by_phone(phone: str) -> dict | None:
    return await execute_one(
        "SELECT * FROM whatsapp.senders WHERE phone_number = $1", phone
    )


async def get_sender_by_subaccount(subaccount_sid: str) -> dict | None:
    """Webhook entry point: Twilio tells us the AccountSid, nothing else."""
    return await execute_one(
        "SELECT * FROM whatsapp.senders WHERE subaccount_sid = $1", subaccount_sid
    )


# ------------------------------------------------------------------- templates

async def get_template(shop_id: UUID, template_key: str) -> dict | None:
    return await execute_one(
        "SELECT * FROM whatsapp.templates WHERE shop_id = $1 AND template_key = $2",
        shop_id, template_key,
    )


async def upsert_template(
    *, shop_id: UUID, template_key: str, content_sid: str,
    category: str, status: str, variable_count: int,
) -> dict:
    row = await execute_one(
        """
        INSERT INTO whatsapp.templates
            (shop_id, template_key, content_sid, category, status, variable_count)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (shop_id, template_key) DO UPDATE
        SET content_sid = EXCLUDED.content_sid,
            category = EXCLUDED.category,
            status = EXCLUDED.status,
            variable_count = EXCLUDED.variable_count,
            rejection_reason = NULL,
            updated_at = now()
        RETURNING *
        """,
        shop_id, template_key, content_sid, category, status, variable_count,
    )
    return row  # type: ignore[return-value]


async def set_template_status(
    *, content_sid: str, status: str, rejection_reason: str | None = None
) -> None:
    await execute_void(
        """
        UPDATE whatsapp.templates
        SET status = $2, rejection_reason = $3, updated_at = now()
        WHERE content_sid = $1
        """,
        content_sid, status, rejection_reason,
    )


async def list_unresolved_templates() -> list[dict]:
    """Templates Meta hasn't ruled on yet — polled by the tick."""
    return await execute(
        """
        SELECT t.*, w.subaccount_sid
        FROM whatsapp.templates t
        JOIN whatsapp.senders w ON w.shop_id = t.shop_id
        WHERE t.status IN ('unsubmitted','received','pending')
          AND w.subaccount_sid IS NOT NULL
        """
    )


# ----------------------------------------------------------------- the queue

async def enqueue(
    *, shop_id: UUID, customer_id: UUID | None, campaign_key: str | None,
    to_phone: str, from_number: str, content_sid: str,
    variables: dict, preview: str, scheduled_at, status: str = "queued",
    suppressed_reason: str | None = None,
) -> UUID | None:
    """Queue one message. Returns None if this campaign already reached them.

    The None is the unique index doing idempotency: a retried or
    double-clicked enqueue is a no-op, not a second message.
    """
    row = await execute_one(
        """
        INSERT INTO whatsapp.outbound_messages
            (shop_id, customer_id, campaign_key, to_phone, from_number,
             content_sid, variables, preview, scheduled_at, status, suppressed_reason)
        VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11)
        ON CONFLICT (shop_id, campaign_key, customer_id)
            WHERE campaign_key IS NOT NULL AND customer_id IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        shop_id, customer_id, campaign_key, to_phone, from_number,
        content_sid, json.dumps(variables), preview, scheduled_at,
        status, suppressed_reason,
    )
    return row["id"] if row else None


async def requeue_stuck(older_than_minutes: int = 60) -> int:
    """Return claimed-but-never-sent rows to the queue.

    Only reachable if a sweep died between claiming a row and calling Twilio.
    Without this the row sits in 'sending' forever and the customer silently
    never hears from the salon.
    """
    rows = await execute(
        """
        UPDATE whatsapp.outbound_messages
        SET status = 'queued', updated_at = now()
        WHERE status = 'sending'
          AND updated_at < now() - make_interval(mins => $1)
        RETURNING id
        """,
        older_than_minutes,
    )
    return len(rows)


async def claim_due(limit: int) -> list[dict]:
    """Atomically claim up to `limit` due messages for online senders.

    The claim (queued -> sending) and the selection are one statement on
    purpose: two overlapping ticks, or two Fly machines, must never both send
    the same row. SKIP LOCKED means the loser takes different work rather
    than blocking.
    """
    return await execute(
        """
        WITH due AS (
            SELECT m.id
            FROM whatsapp.outbound_messages m
            JOIN whatsapp.senders s
              ON s.shop_id = m.shop_id AND s.status = 'online'
            WHERE m.status = 'queued' AND m.scheduled_at <= now()
            ORDER BY m.scheduled_at
            LIMIT $1
            FOR UPDATE OF m SKIP LOCKED
        )
        UPDATE whatsapp.outbound_messages m
        SET status = 'sending', updated_at = now()
        FROM due
        WHERE m.id = due.id
        RETURNING m.*
        """,
        limit,
    )


async def sent_today(shop_id: UUID) -> int:
    """Messages that actually left today, for the per-shop daily cap."""
    row = await execute_one(
        """
        SELECT count(*) AS n FROM whatsapp.outbound_messages
        WHERE shop_id = $1 AND sent_at >= date_trunc('day', now())
        """,
        shop_id,
    )
    return int(row["n"]) if row else 0


async def mark_sent(
    *, message_id: UUID, provider_sid: str, price_usd: float | None, credits: int
) -> None:
    await execute_void(
        """
        UPDATE whatsapp.outbound_messages
        SET status = 'sent', provider_sid = $2, price_usd = $3,
            credits_charged = $4, sent_at = now(), updated_at = now()
        WHERE id = $1
        """,
        message_id, provider_sid, price_usd, credits,
    )


async def mark_failed(*, message_id: UUID, error_code: str) -> None:
    await execute_void(
        """
        UPDATE whatsapp.outbound_messages
        SET status = 'failed', error_code = $2, updated_at = now()
        WHERE id = $1
        """,
        message_id, error_code[:200],
    )


async def mark_suppressed(*, message_id: UUID, reason: str) -> None:
    await execute_void(
        """
        UPDATE whatsapp.outbound_messages
        SET status = 'suppressed', suppressed_reason = $2, updated_at = now()
        WHERE id = $1
        """,
        message_id, reason,
    )


async def requeue_one(*, message_id: UUID, minutes: int) -> None:
    """Push one claimed message back into the queue, later.

    Used when the shop is over its daily cap: the message isn't wrong, it's
    early, and dropping it would silently lose a scheduled promotion.
    """
    await execute_void(
        """
        UPDATE whatsapp.outbound_messages
        SET status = 'queued',
            scheduled_at = greatest(scheduled_at, now()) + make_interval(mins => $2),
            updated_at = now()
        WHERE id = $1
        """,
        message_id, minutes,
    )


async def cancel_queued(*, shop_id: UUID, campaign_key: str) -> int:
    """Cancel a campaign's not-yet-sent messages. Sent rows stay as history."""
    rows = await execute(
        """
        UPDATE whatsapp.outbound_messages
        SET status = 'cancelled', updated_at = now()
        WHERE shop_id = $1 AND campaign_key = $2 AND status IN ('queued','sending')
        RETURNING id
        """,
        shop_id, campaign_key,
    )
    return len(rows)


async def update_status_by_sid(
    *, provider_sid: str, status: str, price_usd: float | None, error_code: str | None
) -> dict | None:
    """Twilio status callback. Returns the row so the caller can act on it."""
    return await execute_one(
        """
        UPDATE whatsapp.outbound_messages
        SET status = $2,
            price_usd = COALESCE($3, price_usd),
            error_code = COALESCE($4, error_code),
            updated_at = now()
        WHERE provider_sid = $1
        RETURNING *
        """,
        provider_sid, status, price_usd, error_code,
    )


async def withdraw_marketing_consent(customer_id: UUID) -> None:
    """Write a WhatsApp opt-out back to the shared consent column.

    Meta puts a native "Stop promotions" button on every marketing template,
    so unlike SMS (where STOP handling was removed on 2026-08-15) WhatsApp
    does give the customer a self-service opt-out. Honouring it here keeps
    the webapp's consent UI — which reads business_app_core directly —
    honest, and stops the next campaign from burning a send on a guaranteed
    63050.
    """
    await execute_void(
        """
        UPDATE business_app_core.customers
        SET marketing_consent = false,
            marketing_consent_withdrawn_at = now(),
            marketing_consent_source = 'whatsapp_opt_out'
        WHERE id = $1
        """,
        customer_id,
    )
