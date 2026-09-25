"""SQL for the `whatsapp` schema. See booking_engine/db/sql/14_whatsapp_schema.sql.

Customer consent is read through `sms_queries.get_customer_for_send` — the
same single source of truth (`business_app_core.customers`) the SMS path uses.
There is deliberately no WhatsApp-specific consent table.
"""
from __future__ import annotations

import json
from uuid import UUID

from booking_engine.config import get_settings
from booking_engine.db.connection import execute, execute_one, execute_void
from booking_engine.services.secret_box import seal, unseal


# --------------------------------------------------------------------- senders
#
# `access_token` is encrypted at rest (services/secret_box.py) and sealed and
# opened *here*, at the one boundary every caller already goes through — so no
# service, route or test had to learn about it. Every read that can surface the
# column passes through `_opened`; a new one that doesn't is a token leaked to
# a log or a Graph call as ciphertext.

def _opened(row: dict | None) -> dict | None:
    if row and row.get("access_token"):
        row["access_token"] = unseal(row["access_token"], get_settings().whatsapp_token_key)
    return row


def _opened_all(rows: list[dict]) -> list[dict]:
    return [_opened(r) for r in rows]


async def get_sender(shop_id: UUID) -> dict | None:
    return _opened(await execute_one(
        "SELECT * FROM whatsapp.senders WHERE shop_id = $1", shop_id
    ))


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
    if "access_token" in fields:
        fields["access_token"] = seal(
            fields["access_token"], get_settings().whatsapp_token_key
        )
    if not fields:
        return
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    verified = ", verified_at = now()" if fields.get("status") == "online" else ""
    await execute_void(
        f"UPDATE whatsapp.senders SET {sets}{verified}, updated_at = now() "
        f"WHERE shop_id = $1",
        shop_id, *fields.values(),
    )


async def delete_sender(shop_id: UUID) -> int:
    """The owner disconnected the WABA. Returns how many queued sends it cancelled.

    One statement so it is all-or-nothing. Templates go with the sender: they
    mirror *that* WABA's approvals, and a reconnect to a different one would
    otherwise skip pushing to it because the rows already read "approved".
    Outbound history stays; only not-yet-sent rows are cancelled, since
    nothing would ever send them. `sending` is left alone — it is mid-flight.
    """
    row = await execute_one(
        """
        WITH cancelled AS (
          UPDATE whatsapp.outbound_messages
          SET status = 'cancelled', updated_at = now()
          WHERE shop_id = $1 AND status = 'queued'
          RETURNING 1
        ), templates AS (
          DELETE FROM whatsapp.templates WHERE shop_id = $1 RETURNING 1
        ), sender AS (
          DELETE FROM whatsapp.senders WHERE shop_id = $1 RETURNING 1
        )
        SELECT (SELECT count(*) FROM cancelled)::int AS cancelled
        """,
        shop_id,
    )
    return row["cancelled"] if row else 0


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
    return _opened_all(await execute(
        "SELECT * FROM whatsapp.senders WHERE status IN ('verifying','pending_signup') "
        "AND phone_number_id IS NOT NULL AND access_token IS NOT NULL"
    ))


async def list_senders_needing_token_reminder(
    *, window_days: int, cooldown_hours: int
) -> list[dict]:
    """Online senders whose token dies soon and who haven't just been told.

    The banner that used to be the only warning is pull-only: the owner has to
    open the app inside the window, and never sees it at all while their
    session is in employee view, since every WhatsApp route is owner-only.

    `token_expires_at IS NOT NULL` matters — NULL means Meta reported no
    expiry, and emailing about a deadline nobody read back from Meta is worse
    than silence. Past the date is deliberately included: that salon is
    already dead and the reconnect still fixes it.
    """
    return await execute(
        """
        SELECT shop_id, phone_number, token_expires_at,
               EXTRACT(DAY FROM token_expires_at - now())::int AS days_left
        FROM whatsapp.senders
        WHERE status = 'online'
          AND token_expires_at IS NOT NULL
          AND token_expires_at < now() + ($1 || ' days')::interval
          AND (token_reminder_sent_at IS NULL
               OR token_reminder_sent_at < now() - ($2 || ' hours')::interval)
        """,
        str(window_days), str(cooldown_hours),
    )


async def mark_token_reminder_sent(shop_id: UUID) -> None:
    """Record the attempt, not the delivery.

    A shop with no owner mailbox would otherwise be retried every hour for the
    whole window against a fact about the shop. The banner still covers it.
    """
    await execute_void(
        "UPDATE whatsapp.senders SET token_reminder_sent_at = now() WHERE shop_id = $1",
        shop_id,
    )


async def get_sender_by_phone(phone: str) -> dict | None:
    return _opened(await execute_one(
        "SELECT * FROM whatsapp.senders WHERE phone_number = $1", phone
    ))


async def get_sender_by_waba(waba_id: str) -> dict | None:
    """Webhook entry point.

    Meta posts every customer's traffic to one app-level URL and identifies
    the tenant only by `entry[].id`, the WABA id — so this is the sole route
    from an inbound webhook to a shop.
    """
    return _opened(await execute_one(
        "SELECT * FROM whatsapp.senders WHERE waba_id = $1", waba_id
    ))


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
) -> bool:
    """Record Meta's verdict on one template. False when we hold no such row.

    Keyed by (shop, name) and not by name alone: every salon's copy of the
    catalogue carries the *same* name (`kairo_promo_v1`), so a global update
    would rule on every shop at once from one shop's webhook.

    Returning whether it matched is what lets the webhook say so. A verdict for
    a template we have no row for used to update nothing and report success —
    the exact shape of the 2026-09-20 failure, where Meta held six templates we
    had never recorded and every signal about them went to ground.
    """
    row = await execute_one(
        """
        UPDATE whatsapp.templates
        SET status = $3, rejection_reason = $4, updated_at = now()
        WHERE shop_id = $1 AND name = $2
        RETURNING template_key
        """,
        shop_id, name, status, rejection_reason,
    )
    return row is not None


async def list_senders_needing_templates(fingerprints: list[str]) -> list[dict]:
    """Live senders missing a pushed template, or holding an outdated body.

    The gap this closes: propagation is gated on Kairo's own copy being
    approved, so a salon that onboards while a template is still pending gets
    nothing. Meta approves ours an hour later and — before this query existed —
    nothing ever went back for that shop. `list_verifying_senders` doesn't
    catch it (that salon is `online`, its sender is fine), onboarding is long
    over, and the panel only offers the manual re-push for a *rejected*
    template, not a missing one. The result was a shop that could never send,
    with nothing anywhere saying why.

    **`key|body_hash`, not a plain count of rows.** A count of any kind cannot
    see a body that changed under an unchanged name, and it was inflated by
    rows for templates the sweep didn't push, so a shop could look complete
    while missing a real one. The caller decides what "complete" means by
    passing `propagation_fingerprints()` — catalogue plus the document
    templates (the receipt is one of the pushed since 2026-09-23) — so this
    query itself stays agnostic about which templates exist.

    Cheap enough to run every tick: one count per sender, and shops holding
    every current fingerprint — which is all of them, steady-state — don't
    come back.
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
    return _opened_all(await execute(
        """
        SELECT s.shop_id, s.waba_id, s.access_token, t.name
        FROM whatsapp.templates t
        JOIN whatsapp.senders s ON s.shop_id = t.shop_id
        WHERE t.template_key = $1
          AND s.waba_id IS NOT NULL AND s.access_token IS NOT NULL
        """,
        template_key,
    ))


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
    return _opened_all(await execute(
        """
        SELECT t.*, w.waba_id, w.access_token
        FROM whatsapp.templates t
        JOIN whatsapp.senders w ON w.shop_id = t.shop_id
        WHERE (
                t.status IN ('unsubmitted','received','pending')
                -- An approved template can still be paused or disabled later,
                -- on Meta's own quality signals, and that verdict arrives by
                -- the same webhook that can be missed. Left out, a missed one
                -- meant sending against a dead template forever with nothing
                -- anywhere saying why. Re-checked daily rather than hourly:
                -- it is one Graph call per template per shop, and a pause is
                -- not an emergency the way a rejection is.
                OR (t.status = 'approved' AND t.updated_at < now() - interval '1 day')
              )
          AND w.waba_id IS NOT NULL AND w.access_token IS NOT NULL
        """
    ))


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


async def claim_due(
    limit: int, *, shop_id: UUID | None = None, campaign_key: str | None = None,
) -> list[dict]:
    """Atomically claim up to `limit` due messages for online senders.

    The claim (queued -> sending) and the selection are one statement on
    purpose: two overlapping ticks, or two Fly machines, must never both send
    the same row. SKIP LOCKED means the loser takes different work rather
    than blocking.

    The template's category rides along so send_due can re-apply consent and
    the cooldown only to MARKETING. A LEFT JOIN (not inner) so a row whose
    template can't be resolved is still claimed — and fails closed to the
    stricter MARKETING checks when category is missing.

    `shop_id` + `campaign_key` narrow the claim to one campaign: the single
    win-back is sent at enqueue time instead of waiting for the tick.

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
              AND ($2::uuid IS NULL OR m.shop_id = $2)
              AND ($3::text IS NULL OR m.campaign_key = $3)
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
        limit, shop_id, campaign_key,
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


async def pending_campaigns(*, shop_id: UUID) -> list[dict]:
    """Every campaign of this shop that still has messages waiting to leave.

    The bulk tile renders this on load, so a scheduled drip stays visible
    after a reload (or on another device) — and drops out on its own once the
    tick has sent the last row.
    """
    return await execute(
        """
        SELECT
          campaign_key,
          count(*) FILTER (WHERE status IN ('queued','sending')) AS pending,
          count(*) FILTER (WHERE status IN ('sent','delivered','read')) AS sent,
          count(*) FILTER (WHERE status = 'failed')     AS failed,
          count(*) FILTER (WHERE status = 'suppressed') AS suppressed,
          min(scheduled_at) FILTER (WHERE status = 'queued') AS next_due_at,
          max(scheduled_at) FILTER (WHERE status = 'queued') AS last_due_at
        FROM whatsapp.outbound_messages
        WHERE shop_id = $1 AND campaign_key IS NOT NULL
        GROUP BY campaign_key
        HAVING count(*) FILTER (WHERE status IN ('queued','sending')) > 0
        ORDER BY min(scheduled_at)
        """,
        shop_id,
    )


async def customer_campaign_messages(*, shop_id: UUID, customer_id: UUID) -> list[dict]:
    """Everything on file about this customer's WhatsApp conversation: every
    message actually sent to them, the campaigns they were assigned to but
    never received (holdout arm), and — now that they write back — every
    message *they* sent us.

    This is the read behind the webapp's Anagrafiche → "Campagne" tab, which
    doubles as the GDPR subject-access artifact: "what do you hold about me".
    A response that showed only our side of a conversation was not one — half
    of it is personal data the customer authored themselves, including a voice
    note's words (`direction = 'in'`, `body` is `coalesce(transcript, body)`:
    the transcript is what we actually hold, and leaving it out would show
    "we have nothing" for a message we have the words of).

    The goal lives in market_intel.campaigns — the campaign data owner — and
    is linked through campaign_key = campaign id, which is the campaign_key
    the webapp passes when it enqueues a campaign. Marketing-engine's schema is
    a read here, exactly as this repo already reads business_app_core.

    One UNION ALL, not three Python-merged queries: `inbound_messages` has no
    campaign to join, so ordering the three shapes consistently belongs in SQL
    rather than as a second, separate sort rule on the Python side. Every
    typed NULL is deliberate — an untyped NULL in a UNION ALL is "could not
    determine data type of parameter" the moment two branches disagree, the
    same class of bug AGENTS.md's 2026-07-18/2026-07-21 entries record for
    ON CONFLICT predicates.
    """
    return await execute(
        """
        SELECT
          om.id AS message_id,
          om.campaign_key,
          om.preview,
          om.preview AS body,
          om.status AS delivery_status,
          om.sent_at,
          om.created_at,
          om.suppressed_reason,
          om.error_code,
          om.scheduled_at,
          c.goal,
          c.personalization,
          'send' AS arm,
          'out' AS direction
        FROM whatsapp.outbound_messages om
        LEFT JOIN market_intel.campaigns c
          ON c.shop_id = om.shop_id AND c.id::text = om.campaign_key
        WHERE om.shop_id = $1 AND om.customer_id = $2

        UNION ALL

        SELECT
          NULL::uuid AS message_id,
          cr.campaign_id::text AS campaign_key,
          cr.preview,
          cr.preview AS body,
          NULL::text AS delivery_status,
          NULL::timestamptz AS sent_at,
          c.created_at,
          NULL::text AS suppressed_reason,
          NULL::text AS error_code,
          NULL::timestamptz AS scheduled_at,
          c.goal,
          c.personalization,
          cr.arm,
          NULL::text AS direction
        FROM market_intel.campaign_recipients cr
        JOIN market_intel.campaigns c ON c.id = cr.campaign_id
        WHERE cr.customer_id = $2 AND c.shop_id = $1 AND cr.arm = 'holdout'

        UNION ALL

        SELECT
          i.id AS message_id,
          NULL::text AS campaign_key,
          coalesce(i.transcript, i.body) AS preview,
          coalesce(i.transcript, i.body) AS body,
          NULL::text AS delivery_status,
          NULL::timestamptz AS sent_at,
          i.received_at AS created_at,
          NULL::text AS suppressed_reason,
          NULL::text AS error_code,
          NULL::timestamptz AS scheduled_at,
          NULL::text AS goal,
          NULL::text AS personalization,
          NULL::text AS arm,
          'in' AS direction
        FROM whatsapp.inbound_messages i
        WHERE i.shop_id = $1 AND i.customer_id = $2

        ORDER BY created_at DESC
        """,
        shop_id, customer_id,
    )


async def record_inbound(
    *,
    shop_id: UUID,
    from_phone: str,
    body: str,
    message_type: str,
    wa_message_id: str | None = None,
    intent: str | None = None,
    confidence: float | None = None,
) -> dict | None:
    """Persist one inbound reply. Returns the row, or None if it is a replay.

    The webhook previously logged and discarded these; campaign measurement
    (design §9, "replies within 72h") needs them as a queryable signal. A reply
    is linked back to the message it answers by phone — from_phone of the reply
    equals to_phone of the sent message — so no sender identity is needed here.

    `wa_message_id` is Meta's `wamid`, and the dedup key: Meta retries a webhook
    it believes failed, and without this a retry is a second bubble in the
    thread and a second AI classification that costs real money. Returning
    None on the conflict answers "have we already processed this?" in the same
    statement — one question, one round trip, no check-then-act race.

    **The ON CONFLICT predicate is not optional.** `inbound_messages_wa_id_uniq`
    is a *partial* index (`WHERE wa_message_id IS NOT NULL`, migration 24), and
    Postgres cannot infer a partial index unless the clause repeats its
    predicate — without it the statement fails outright with "no unique or
    exclusion constraint matching the ON CONFLICT specification". This repo has
    been bitten by exactly that twice; see AGENTS.md 2026-07-18 and 2026-07-21.
    A NULL id therefore conflicts with nothing, which is the point: a message
    Meta sent us without an id still gets recorded, every time.

    `intent`/`confidence` are filled here only for a tap on a button we
    defined — the id *is* the intent, so there is nothing for a model to rule
    on. Everything typed arrives with both NULL, for the classifier.
    """
    return await execute_one(
        """
        INSERT INTO whatsapp.inbound_messages
          (shop_id, from_phone, body, message_type, wa_message_id, intent, confidence)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT NULL DO NOTHING
        RETURNING *
        """,
        shop_id, from_phone, body, message_type, wa_message_id, intent, confidence,
    )


async def record_echo(
    *,
    shop_id: UUID,
    to_phone: str,
    from_number: str,
    body: str,
    wa_message_id: str | None = None,
) -> dict | None:
    """Record a message the owner sent from their own WhatsApp Business App.

    Every sender is coexistence: the number is still live on the owner's phone
    and they answer from there, which Meta reports on its own webhook field.
    Stored as an outbound row with `origin = 'phone'` (migration 24) so the
    thread is a whole conversation rather than our half of one.

    Two things it deliberately is not:

    - It is **not** a send of ours: no template, no campaign, no provider
      status lifecycle to follow. It lands as `sent`, once, and stays there.
    - It does **not** touch `inbound_messages`, and so cannot extend the 24h
      service window. That window is driven by *customer* inbound alone; an
      echo that extended it would let us send into a conversation Meta
      considers closed, which comes back as an opaque provider error long
      after the cause.

    It does clear the unread state on the thread — the owner has answered, and
    the Inbox must not keep asking them to.

    Dedup is best-effort: `provider_sid` carries the wamid but has only a plain
    index behind it, so a genuinely concurrent retry could still double-write.
    A duplicate bubble in a thread is cosmetic; the inbound path, where a
    duplicate costs an AI call, is the one guarded by a unique index.
    """
    row = await execute_one(
        """
        INSERT INTO whatsapp.outbound_messages
          (shop_id, to_phone, from_number, preview, provider_sid,
           origin, status, sent_at)
        SELECT $1, $2, $3, $4, $5::text, 'phone', 'sent', now()
        WHERE $5::text IS NULL OR NOT EXISTS (
          SELECT 1 FROM whatsapp.outbound_messages
          WHERE provider_sid = $5::text AND origin = 'phone'
        )
        RETURNING *
        """,
        shop_id, to_phone, from_number, body, wa_message_id or None,
    )
    await execute_void(
        """
        UPDATE whatsapp.inbound_messages
        SET read_at = now()
        WHERE shop_id = $1
          AND ltrim(from_phone, '+') = ltrim($2, '+')
          AND read_at IS NULL
        """,
        shop_id, to_phone,
    )
    return row


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


# ------------------------------------------------------------------- retention
#
# One statement, both directions, bounded by threads.
#
# **Both halves go together or neither does.** Half a conversation is still
# personal data and is no longer readable as a conversation, so the two DELETEs
# are CTEs of a single statement: they share one snapshot of `expired` and
# commit together. Limiting each table independently would not do — a batch
# that happened to fill up on inbound would leave that thread's outbound behind
# until some later run.
#
# The batch is therefore counted in **threads**, not rows: the unit that must
# not be split is the conversation. Within a chosen thread every expired row on
# both sides goes, and everything newer than the cutoff stays — that is the
# policy (a conversation truncated at six months), not a halving.
#
# Re-running immediately is a no-op by construction: the predicate is a
# timestamp comparison against rows that no longer exist.
#
# The cutoff is `<=`, matching `wa_retention.is_expired`'s `>=` — a row that
# has reached exactly six months has had its six months.
_PURGE = """
WITH expired AS (
  SELECT shop_id, ltrim(from_phone, '+') AS key
    FROM whatsapp.inbound_messages
   WHERE received_at <= $1
  UNION
  SELECT shop_id, ltrim(to_phone, '+') AS key
    FROM whatsapp.outbound_messages
   WHERE coalesce(sent_at, created_at) <= $1
   ORDER BY 1, 2
   LIMIT $2
), gone_in AS (
  DELETE FROM whatsapp.inbound_messages i
   USING expired e
   WHERE i.shop_id = e.shop_id
     AND ltrim(i.from_phone, '+') = e.key
     AND i.received_at <= $1
  RETURNING 1
), gone_out AS (
  DELETE FROM whatsapp.outbound_messages o
   USING expired e
   WHERE o.shop_id = e.shop_id
     AND ltrim(o.to_phone, '+') = e.key
     AND coalesce(o.sent_at, o.created_at) <= $1
  RETURNING 1
)
SELECT (SELECT count(*) FROM expired)  AS threads,
       (SELECT count(*) FROM gone_in)  AS inbound,
       (SELECT count(*) FROM gone_out) AS outbound
"""


async def purge_expired_threads(*, cutoff, threads: int) -> dict:
    """Delete every message older than `cutoff`, for at most `threads` threads.

    Returns {'threads', 'inbound', 'outbound'} — how much this run actually
    removed, which is what the tick reports.

    `voice_agent.calls` is deliberately untouched: that row is the business
    record of an appointment being made, it outlives the chat that produced it,
    and it is not this policy's to expire.
    """
    row = await execute_one(_PURGE, cutoff, threads)
    return dict(row or {"threads": 0, "inbound": 0, "outbound": 0})
