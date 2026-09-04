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


async def get_shop_language(shop_id: UUID) -> str | None:
    """The locale the shop runs the platform in — `shops.language`, NOT NULL.

    It is what template names are composed from, so it is read at the moment
    of use rather than copied onto the sender: a shop that switches locale
    must start addressing the other language's templates immediately.
    """
    row = await execute_one(
        "SELECT language FROM business_app_core.shops WHERE id = $1", shop_id
    )
    return (row or {}).get("language")


async def upsert_sender(*, shop_id: UUID, display_name: str, source: str) -> dict:
    row = await execute_one(
        """
        INSERT INTO whatsapp.senders (shop_id, display_name, source)
        VALUES ($1,$2,$3)
        ON CONFLICT (shop_id) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            source = EXCLUDED.source,
            updated_at = now()
        RETURNING *
        """,
        shop_id, display_name, source,
    )
    return row  # type: ignore[return-value]


async def set_sender_fields(shop_id: UUID, **fields) -> None:
    """Persist whatever we just learned, one column at a time.

    Same shape as number_request_queries.set_sids: every Meta identifier is
    written the moment it exists, so a crash halfway through onboarding leaves
    a resumable row rather than a WABA we're subscribed to and can't find.
    """
    allowed = {
        "status", "waba_id", "phone_number_id", "access_token", "platform_type",
        "token_expires_at", "phone_number", "display_name", "quality_rating",
        "messaging_limit", "throughput_level", "offline_reason", "daily_cap",
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


async def delete_pending_sender(shop_id: UUID) -> None:
    """Drop an abandoned onboarding row.

    `start()` wrote it to record intent; nothing completed it, so it must not
    linger as a permanent `pending_signup`. Scoped to that status on purpose:
    an `online` or `failed` sender is real state and must survive.
    """
    await execute_void(
        "DELETE FROM whatsapp.senders WHERE shop_id = $1 AND status = 'pending_signup'",
        shop_id,
    )


async def list_verifying_senders() -> list[dict]:
    """Senders not yet online — polled by the tick to pick up Meta's verdict.

    A normal coexistence onboarding lands `online` in one round trip and never
    shows up here. This catches the one abnormal case: `complete()` crashed
    after persisting the token/waba/phone_number_id but before flipping status
    to `online` (or `failed`) — the row it left behind is exactly this shape.
    """
    return await execute(
        "SELECT * FROM whatsapp.senders WHERE status IN ('verifying','pending_signup') "
        "AND phone_number_id IS NOT NULL AND access_token IS NOT NULL"
    )


async def get_sender_by_phone(phone: str) -> dict | None:
    return await execute_one(
        "SELECT * FROM whatsapp.senders WHERE phone_number = $1", phone
    )


async def get_sender_by_waba(waba_id: str) -> dict | None:
    """Webhook entry point.

    Meta posts every customer's traffic to one app-level URL and identifies
    the tenant only by `entry[].id`, the WABA id — so this is the sole route
    from an inbound webhook to a shop.
    """
    return await execute_one(
        "SELECT * FROM whatsapp.senders WHERE waba_id = $1", waba_id
    )


# ------------------------------------------------------------------- templates

async def get_template(shop_id: UUID, template_key: str) -> dict | None:
    return await execute_one(
        "SELECT * FROM whatsapp.templates WHERE shop_id = $1 AND template_key = $2",
        shop_id, template_key,
    )


async def upsert_template(
    *, shop_id: UUID, template_key: str, name: str, meta_template_id: str,
    language: str, category: str, status: str, variable_count: int,
    body_hash: str | None = None,
) -> dict:
    """Record what this WABA holds. `body_hash` is *which version* of the copy.

    Without it, `status = 'approved'` says a template with that name passed
    review — not that the salon is sending the text this repo currently
    defines. It is the whole drift signal `ensure_templates` reads.
    """
    row = await execute_one(
        """
        INSERT INTO whatsapp.templates
            (shop_id, template_key, name, meta_template_id, language,
             category, status, variable_count, body_hash)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        ON CONFLICT (shop_id, template_key) DO UPDATE
        SET name = EXCLUDED.name,
            meta_template_id = EXCLUDED.meta_template_id,
            language = EXCLUDED.language,
            category = EXCLUDED.category,
            status = EXCLUDED.status,
            variable_count = EXCLUDED.variable_count,
            body_hash = EXCLUDED.body_hash,
            rejection_reason = NULL,
            updated_at = now()
        RETURNING *
        """,
        shop_id, template_key, name, meta_template_id, language,
        category, status, variable_count, body_hash,
    )
    return row  # type: ignore[return-value]


async def set_template_status(
    *, shop_id: UUID, name: str, status: str, rejection_reason: str | None = None
) -> None:
    """Record Meta's verdict on one template.

    Keyed by (shop, name) and not by name alone: every salon's copy of the
    catalogue carries the *same* name (`kairo_promo_v1`), so a global update
    would rule on every shop at once from one shop's webhook.
    """
    await execute_void(
        """
        UPDATE whatsapp.templates
        SET status = $3, rejection_reason = $4, updated_at = now()
        WHERE shop_id = $1 AND name = $2
        """,
        shop_id, name, status, rejection_reason,
    )


async def list_senders_needing_templates(fingerprints: list[str]) -> list[dict]:
    """Live senders missing a catalogue entry, or holding an outdated body.

    The gap this closes: propagation is gated on Kairo's own copy being
    approved, so a salon that onboards while a template is still pending gets
    nothing. Meta approves ours an hour later and — before this query existed —
    nothing ever went back for that shop. `list_verifying_senders` doesn't
    catch it (that salon is `online`, its sender is fine), onboarding is long
    over, and the panel only offers the manual re-push for a *rejected*
    template, not a missing one. The result was a shop that could never send,
    with nothing anywhere saying why.

    **`key|body_hash`, not a plain count of rows.** Two bugs in one shape:
    counting rows meant a non-catalogue template (`purchase_receipt_1`, which
    is a document header and deliberately outside `CATALOGUE`) padded the total,
    so a shop with the receipt and one real template missing counted as complete
    and was never revisited. And a count of any kind cannot see a body that
    changed under an unchanged name.

    Cheap enough to run every tick: one count per sender, and shops holding the
    current catalogue — which is all of them, steady-state — don't come back.
    """
    return await execute(
        """
        SELECT s.* FROM whatsapp.senders s
        WHERE s.status = 'online'
          AND s.waba_id IS NOT NULL AND s.access_token IS NOT NULL
          AND (SELECT count(*) FROM whatsapp.templates t
               WHERE t.shop_id = s.shop_id
                 AND t.template_key || '|' || coalesce(t.body_hash, '') = ANY($1)
              ) < cardinality($1::text[])
        """,
        fingerprints,
    )


async def list_senders_with_template(template_key: str) -> list[dict]:
    """Every WABA carrying one catalogue entry — the retire fan-out's worklist."""
    return await execute(
        """
        SELECT s.shop_id, s.waba_id, s.access_token, t.name
        FROM whatsapp.templates t
        JOIN whatsapp.senders s ON s.shop_id = t.shop_id
        WHERE t.template_key = $1
          AND s.waba_id IS NOT NULL AND s.access_token IS NOT NULL
        """,
        template_key,
    )


async def delete_template_row(*, shop_id: UUID, template_key: str) -> None:
    """Drop our record of a template, once Meta no longer has it.

    Deliberately a real delete and not a status flag: `ensure_templates` skips
    any key it already has a row for, so a tombstone would block the shop from
    ever receiving the replacement.
    """
    await execute_void(
        "DELETE FROM whatsapp.templates WHERE shop_id = $1 AND template_key = $2",
        shop_id, template_key,
    )


async def list_unresolved_templates() -> list[dict]:
    """Templates Meta hasn't ruled on yet — the tick's reconciler.

    Verdicts normally arrive within minutes as `message_template_status_update`
    webhooks; this exists because a missed webhook would otherwise leave a
    template `pending` forever and block every send for that shop in silence.
    """
    return await execute(
        """
        SELECT t.*, w.waba_id, w.access_token
        FROM whatsapp.templates t
        JOIN whatsapp.senders w ON w.shop_id = t.shop_id
        WHERE t.status IN ('unsubmitted','received','pending')
          AND w.waba_id IS NOT NULL AND w.access_token IS NOT NULL
        """
    )


# ----------------------------------------------------------------- the queue

async def enqueue(
    *, shop_id: UUID, customer_id: UUID | None, campaign_key: str | None,
    to_phone: str, from_number: str, template_name: str, template_language: str,
    variables: dict, preview: str, scheduled_at, status: str = "queued",
    suppressed_reason: str | None = None,
    initiated_by: UUID | None = None,
) -> UUID | None:
    """Queue one message. Returns None if this campaign already reached them.

    The None is the unique index doing idempotency: a retried or
    double-clicked enqueue is a no-op, not a second message. That guard earns
    its keep on the bulk path, where "invia a 400 clienti" is exactly the
    button someone double-clicks.

    `initiated_by` is the staff id who queued it (NULL for the tick's
    automation sends, which have no human in the path).
    """
    row = await execute_one(
        """
        INSERT INTO whatsapp.outbound_messages
            (shop_id, customer_id, campaign_key, to_phone, from_number,
             template_name, template_language, variables, preview,
             scheduled_at, status, suppressed_reason, initiated_by)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13)
        ON CONFLICT (shop_id, campaign_key, customer_id)
            WHERE campaign_key IS NOT NULL AND customer_id IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        shop_id, customer_id, campaign_key, to_phone, from_number,
        template_name, template_language, json.dumps(variables), preview,
        scheduled_at, status, suppressed_reason, initiated_by,
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

    The template's category rides along so send_due can re-apply consent and
    the cooldown only to MARKETING. A LEFT JOIN (not inner) so a row whose
    template can't be resolved is still claimed — and fails closed to the
    stricter MARKETING checks when category is missing.

    The category is resolved inside the CTE and carried out through `due`, not
    joined again in the UPDATE's FROM: Postgres refuses to let an outer join in
    an UPDATE ... FROM reference the update target ("invalid reference to
    FROM-clause entry for table m"), which is a parse error, so every send
    would fail at runtime rather than at import.
    """
    return await execute(
        """
        WITH due AS (
            SELECT m.id, t.category
            FROM whatsapp.outbound_messages m
            JOIN whatsapp.senders s
              ON s.shop_id = m.shop_id AND s.status = 'online'
            LEFT JOIN whatsapp.templates t
              ON t.shop_id = m.shop_id AND t.name = m.template_name
            WHERE m.status = 'queued' AND m.scheduled_at <= now()
            ORDER BY m.scheduled_at
            LIMIT $1
            FOR UPDATE OF m SKIP LOCKED
        )
        UPDATE whatsapp.outbound_messages m
        SET status = 'sending', updated_at = now()
        FROM due
        WHERE m.id = due.id
        RETURNING m.*, due.category AS category
        """,
        limit,
    )


async def sent_last_24h(shop_id: UUID) -> int:
    """Messages that left in the last **rolling** 24 hours.

    This is the window Meta's messaging-limit tier is measured in, and it is
    deliberately not `sent_today`. A calendar-day count resets at midnight, so
    a sender at its tier ceiling at 23:00 would be handed a fresh allowance
    ninety minutes later and hand Meta nearly two tiers' worth of traffic
    inside one of Meta's windows. The tier check must use Meta's clock.
    """
    row = await execute_one(
        """
        SELECT count(*) AS n FROM whatsapp.outbound_messages
        WHERE shop_id = $1 AND sent_at >= now() - interval '24 hours'
        """,
        shop_id,
    )
    return int(row["n"]) if row else 0


# Marketing-only counting.
#
# **The distinction that must not be collapsed:** Meta's messaging-limit tier
# counts *every* business-initiated conversation, utility templates included —
# so `sent_last_24h`, which enforces that ceiling, deliberately does NOT use
# this filter. Everything below is either an owner-facing counter or the
# per-recipient marketing cooldown, and for those a reminder is not a
# promotion: an appointment confirmation must never consume the campaign
# counter, nor block next week's offer.
#
# Nothing fails when this is wrong. The numbers are just quietly incorrect,
# which is why it is spelled out here rather than inlined three times.
# Joined on (shop_id, name): migration 15 dropped content_sid, and Meta
# addresses a template by name + language, so the name is the load-bearing
# column. Every salon's copy of the catalogue shares the same name, hence the
# shop_id in the join.
_MARKETING_JOIN = """
    JOIN whatsapp.templates t
      ON t.shop_id = om.shop_id AND t.name = om.template_name
     AND t.category = 'MARKETING'
"""


async def sent_today(shop_id: UUID) -> int:
    """Marketing messages that left today, for the owner's counter.

    Calendar-day on purpose — "quanti ne ho mandati oggi" is what the owner
    means. Never use this for a Meta ceiling; see `sent_last_24h`.
    """
    row = await execute_one(
        f"""
        SELECT count(*) AS n FROM whatsapp.outbound_messages om
        {_MARKETING_JOIN}
        WHERE om.shop_id = $1 AND om.sent_at >= date_trunc('day', now())
        """,
        shop_id,
    )
    return int(row["n"]) if row else 0


async def recently_contacted(
    *, shop_id: UUID, customer_ids: list[UUID], hours: int
) -> set[UUID]:
    """Which of these customers already got a marketing message in `hours`.

    Our own guard against Meta's per-user, cross-brand marketing cap (131049),
    which is otherwise only discoverable *after* burning the send and taking
    the quality-rating hit. One batched query rather than one per recipient:
    a bulk campaign is up to 2000 of them.
    """
    if not customer_ids:
        return set()
    rows = await execute(
        f"""
        SELECT DISTINCT om.customer_id FROM whatsapp.outbound_messages om
        {_MARKETING_JOIN}
        WHERE om.shop_id = $1
          AND om.customer_id = ANY($2::uuid[])
          AND om.sent_at >= now() - make_interval(hours => $3)
        """,
        shop_id, customer_ids, hours,
    )
    return {row["customer_id"] for row in rows}


async def onboarded_last_7_days() -> int:
    """Senders that went live in the last rolling 7 days, across all shops.

    Meta caps a Tech Provider at 10 new customers per rolling 7 days (200 once
    Access Verification is complete). Exceeding it makes the next salon's
    onboarding fail at Meta with nothing in our logs explaining why.
    """
    row = await execute_one(
        """
        SELECT count(*) AS n FROM whatsapp.senders
        WHERE verified_at >= now() - interval '7 days'
        """
    )
    return int(row["n"]) if row else 0


async def sent_this_month(shop_id: UUID) -> int:
    """Marketing messages that left this calendar month, for the owner's counter.

    Counts `sent_at`, not `created_at`: a queued row that never went out (no
    consent, cancelled, cooled off) is not something the owner sent.
    """
    row = await execute_one(
        f"""
        SELECT count(*) AS n FROM whatsapp.outbound_messages om
        {_MARKETING_JOIN}
        WHERE om.shop_id = $1 AND om.sent_at >= date_trunc('month', now())
        """,
        shop_id,
    )
    return int(row["n"]) if row else 0


async def mark_sent(
    *, message_id: UUID, provider_sid: str, price_usd: float | None,
    credits: int | None = None,
) -> None:
    """`provider_sid` is Meta's `wamid`; `price_usd` our own send-time estimate.

    `credits` stays None on this channel — the salon pays Meta directly.
    """
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
    *, provider_sid: str, status: str, error_code: str | None
) -> dict | None:
    """Meta status webhook, keyed on the `wamid`. Returns the row to act on.

    No price argument: Meta bills the salon directly and reports no amount
    here, so `price_usd` keeps the estimate written at send time rather than
    being corrected later the way the SMS path's is.
    """
    return await execute_one(
        """
        UPDATE whatsapp.outbound_messages
        SET status = $2,
            error_code = COALESCE($3, error_code),
            updated_at = now()
        WHERE provider_sid = $1
        RETURNING *
        """,
        provider_sid, status, error_code,
    )


async def campaign_progress(*, shop_id: UUID, campaign_key: str) -> dict:
    """Counts per status for one campaign, plus when the last one is due.

    The bulk tile polls this: a campaign that drips over several days is
    otherwise invisible between "inviata" and whatever arrives days later.
    """
    row = await execute_one(
        """
        SELECT
          count(*) FILTER (WHERE status IN ('queued','sending')) AS pending,
          count(*) FILTER (WHERE status IN ('sent','delivered','read')) AS sent,
          count(*) FILTER (WHERE status = 'failed')      AS failed,
          count(*) FILTER (WHERE status = 'suppressed')  AS suppressed,
          count(*) FILTER (WHERE status = 'cancelled')   AS cancelled,
          max(scheduled_at) FILTER (WHERE status = 'queued') AS last_due_at
        FROM whatsapp.outbound_messages
        WHERE shop_id = $1 AND campaign_key = $2
        """,
        shop_id, campaign_key,
    )
    return dict(row) if row else {}


async def customer_campaign_messages(*, shop_id: UUID, customer_id: UUID) -> list[dict]:
    """Everything a customer was part of: every message actually sent to them,
    plus the campaigns they were assigned to but never received (holdout arm).

    This is the read behind the webapp's Anagrafiche → "Campagne" tab, which
    doubles as the GDPR subject-access artifact: "what did you send me, and
    when". The goal lives in market_intel.campaigns — the campaign data owner —
    and is linked through campaign_key = campaign id, which is the campaign_key
    the webapp passes when it enqueues a campaign. Marketing-engine's schema is
    a read here, exactly as this repo already reads business_app_core.
    """
    messages = await execute(
        """
        SELECT
          om.id AS message_id,
          om.campaign_key,
          om.preview,
          om.status AS delivery_status,
          om.sent_at,
          om.created_at,
          om.suppressed_reason,
          c.goal,
          c.personalization,
          'send' AS arm
        FROM whatsapp.outbound_messages om
        LEFT JOIN market_intel.campaigns c
          ON c.shop_id = om.shop_id AND c.id::text = om.campaign_key
        WHERE om.shop_id = $1 AND om.customer_id = $2
        """,
        shop_id, customer_id,
    )
    holdout = await execute(
        """
        SELECT
          NULL::uuid AS message_id,
          cr.campaign_id::text AS campaign_key,
          cr.preview,
          NULL::text AS delivery_status,
          NULL::timestamptz AS sent_at,
          c.created_at,
          NULL::text AS suppressed_reason,
          c.goal,
          c.personalization,
          cr.arm
        FROM market_intel.campaign_recipients cr
        JOIN market_intel.campaigns c ON c.id = cr.campaign_id
        WHERE cr.customer_id = $1 AND c.shop_id = $2 AND cr.arm = 'holdout'
        """,
        customer_id, shop_id,
    )
    rows = messages + holdout
    rows.sort(key=lambda r: (r.get("created_at") or ""), reverse=True)
    return rows


async def record_inbound(*, shop_id: UUID, from_phone: str, body: str, message_type: str) -> None:
    """Persist one inbound reply.

    The webhook previously logged and discarded these; campaign measurement
    (design §9, "replies within 72h") needs them as a queryable signal. A reply
    is linked back to the message it answers by phone — from_phone of the reply
    equals to_phone of the sent message — so no sender identity is needed here.
    """
    await execute_void(
        """
        INSERT INTO whatsapp.inbound_messages (shop_id, from_phone, body, message_type)
        VALUES ($1, $2, $3, $4)
        """,
        shop_id, from_phone, body, message_type,
    )


async def withdraw_marketing_consent(customer_id: UUID) -> None:
    """Write a WhatsApp opt-out back to the shared consent column.

    Meta puts a native "Stop promotions" button on every marketing template,
    so unlike SMS (where STOP handling was removed on 2026-08-15) WhatsApp
    does give the customer a self-service opt-out. Honouring it here keeps
    the webapp's consent UI — which reads business_app_core directly —
    honest, and stops the next campaign from burning a send on a guaranteed
    131050. (That is Meta's opt-out code — the Twilio-era 63033/63050 are
    gone. Note it is *not* 131049, the cross-brand frequency cap, which is a
    cooldown and must never reach this function.)
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
