"""DB access for voice_agent.number_requests.

Tracks the regulatory-bundle lifecycle for self-service number
provisioning: a request row exists long before a number does (Twilio
review can take several days). See
booking_engine/db/sql/12_number_requests.sql.
"""
from __future__ import annotations

import json
from uuid import UUID

from booking_engine.db.connection import execute, execute_one, execute_void


async def get_request(shop_id: UUID) -> dict | None:
    return await execute_one(
        "SELECT * FROM voice_agent.number_requests WHERE shop_id = $1",
        shop_id,
    )


async def upsert_request(
    *,
    shop_id: UUID,
    business_name: str,
    contact_email: str,
    regulation_sid: str | None = None,
    status: str = "draft",
) -> dict:
    """Create or replace a shop's request.

    Unlike insert_telephony (voice_telephony_queries.py), this IS an
    upsert: re-submitting a request legitimately replaces an existing
    draft, and no money has been spent yet at this stage. The
    insert-only rule exists specifically to protect a purchased number
    from being silently orphaned — this table never holds one.
    """
    return await execute_one(
        """
        INSERT INTO voice_agent.number_requests
            (shop_id, business_name, contact_email, regulation_sid, status)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (shop_id) DO UPDATE SET
            business_name = EXCLUDED.business_name,
            contact_email = EXCLUDED.contact_email,
            regulation_sid = EXCLUDED.regulation_sid,
            status = EXCLUDED.status,
            updated_at = now()
        RETURNING *
        """,
        shop_id, business_name, contact_email, regulation_sid, status,
    )


async def set_sids(
    *,
    shop_id: UUID,
    end_user_sid: str | None = None,
    document_sid: str | None = None,
    bundle_sid: str | None = None,
) -> None:
    """Persist whichever Twilio SIDs have been created so far.

    Only the non-None ones are written (via COALESCE, so a partial call
    can't blank a SID that was already stored). Persisting each SID as
    soon as it exists is what makes a half-finished submission resumable
    instead of leaving orphaned Twilio objects we hold no reference to.
    """
    await execute_void(
        """
        UPDATE voice_agent.number_requests
        SET end_user_sid = COALESCE($2, end_user_sid),
            document_sid = COALESCE($3, document_sid),
            bundle_sid = COALESCE($4, bundle_sid),
            updated_at = now()
        WHERE shop_id = $1
        """,
        shop_id, end_user_sid, document_sid, bundle_sid,
    )


async def set_status(
    *,
    shop_id: UUID,
    status: str,
    evaluation_errors: object | None = None,
    rejection_reason: str | None = None,
    submitted_at_now: bool = False,
    reviewed_at_now: bool = False,
) -> None:
    """Transition a request's status.

    submitted_at/reviewed_at are only stamped when the matching *_now
    flag is set, so a status update that doesn't cross that milestone
    leaves the existing timestamp alone. evaluation_errors/
    rejection_reason are written as NULL (not the JSON string "null")
    when cleared, so a cleared error field reads as genuinely absent.
    """
    evaluation_errors_json = (
        json.dumps(evaluation_errors) if evaluation_errors is not None else None
    )
    await execute_void(
        """
        UPDATE voice_agent.number_requests
        SET status = $2,
            evaluation_errors = $3::jsonb,
            rejection_reason = $4,
            submitted_at = CASE WHEN $5 THEN now() ELSE submitted_at END,
            reviewed_at = CASE WHEN $6 THEN now() ELSE reviewed_at END,
            updated_at = now()
        WHERE shop_id = $1
        """,
        shop_id, status, evaluation_errors_json, rejection_reason,
        submitted_at_now, reviewed_at_now,
    )


async def list_pending_review() -> list[dict]:
    """Rows the hourly Twilio-evaluation poll needs to check.

    Matches the partial index on status = 'pending_review' created in
    migration 12, keeping this scan cheap.
    """
    return await execute(
        "SELECT shop_id, bundle_sid FROM voice_agent.number_requests "
        "WHERE status = 'pending_review'"
    )


async def list_provisioned_numbers() -> list[dict]:
    """Every shop with a live number, for the health check."""
    return await execute(
        "SELECT shop_id, kairo_number, kairo_number_sid "
        "FROM voice_agent.shop_telephony"
    )


async def set_health(*, shop_id: UUID, status: str | None, detail: str | None) -> None:
    """Record the result of a number health probe.

    health_checked_at is always updated, so we always record that a
    check ran. health_status is only overwritten when status is not
    None: a None status means the probe was inconclusive (e.g. Twilio
    unreachable), and a provider outage must not repaint every salon's
    number red — the previous verdict stands while we still record the
    attempt.
    """
    await execute_void(
        """
        UPDATE voice_agent.shop_telephony
        SET health_status = COALESCE($2, health_status),
            health_detail = $3,
            health_checked_at = now()
        WHERE shop_id = $1
        """,
        shop_id, status, detail,
    )
