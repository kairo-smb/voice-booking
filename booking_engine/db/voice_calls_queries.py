"""DB access for voice_agent.calls, call_turns, callback_memos."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from booking_engine.db import connection


async def insert_call(
    *, shop_id: UUID, caller_phone: str | None,
    matched_customer_id: UUID | None,
) -> UUID:
    row = await connection.execute_one(
        """
        INSERT INTO voice_agent.calls
            (shop_id, caller_number, matched_customer_id, customer_match, started_at)
        VALUES ($1, $2, $3, $4, now())
        RETURNING id
        """,
        shop_id, caller_phone or "anonymous", matched_customer_id,
        "existing" if matched_customer_id else "unmatched",
    )
    return row["id"]


async def get_call(call_id: UUID) -> dict | None:
    return await connection.execute_one(
        "SELECT * FROM voice_agent.calls WHERE id = $1", call_id,
    )


async def set_call_outcome(
    *, call_id: UUID, outcome: str, summary: str,
    callback_window: str | None,
) -> None:
    await connection.execute_void(
        """
        UPDATE voice_agent.calls
        SET outcome = $2, summary = $3, outcome_reason = $4
        WHERE id = $1
        """,
        call_id, outcome, summary, callback_window,
    )


async def record_session_outcome(*, call_id: UUID, outcome: str, summary: str) -> None:
    """The outcome of a session's work — never a step down from escalated.

    marketing-engine posts this automatically after every successful booking
    write, so it can land after the owner took the thread over mid-turn
    (`mark_escalated` via an echo). `outcome = 'escalated'` IS that thread's
    takeover flag: overwriting it with 'booked' would hand the conversation
    back to the agent, which then talks over the owner. So an escalated row
    keeps its outcome and its `outcome_reason` (the takeover reason or the
    callback window), and an empty summary never erases a written one — the
    escalation's customer message is what the owner reads in the Inbox.
    """
    await connection.execute_void(
        """
        UPDATE voice_agent.calls
        SET outcome = CASE WHEN outcome = 'escalated' THEN outcome ELSE $2 END,
            summary = coalesce(nullif($3, ''), summary)
        WHERE id = $1
        """,
        call_id, outcome, summary,
    )


async def get_appointment_shop_id(*, appointment_id: UUID) -> UUID | None:
    """Which shop owns this appointment. None when there is no such appointment."""
    row = await connection.execute_one(
        "SELECT shop_id FROM business_app_core.appointments WHERE id = $1",
        appointment_id,
    )
    return row["shop_id"] if row else None


async def attach_appointment_to_call(
    *, call_id: UUID, appointment_id: UUID, created: bool,
) -> None:
    """Record the appointment a session booked, moved or cancelled.

    `calls.appointment_id` is what readers use (the webapp's call list,
    `whatsapp.interaction_history`): the latest appointment the session acted
    on. `created` — the session *made* it — also sets `created_booking_id` and
    the appointment's `voice_call_id`, the SQL the deleted
    `voice_tool_queries.attach_booking_to_call` ran on a voice booking. The
    appointment's link is only ever filled, never moved: a later session that
    reschedules or cancels it is not where it came from.
    """
    await connection.execute_void(
        """
        UPDATE voice_agent.calls
        SET appointment_id = $2,
            created_booking_id = CASE WHEN $3 THEN $2 ELSE created_booking_id END
        WHERE id = $1
        """,
        call_id, appointment_id, created,
    )
    if created:
        await connection.execute_void(
            "UPDATE business_app_core.appointments SET voice_call_id = $1 "
            "WHERE id = $2 AND voice_call_id IS NULL",
            call_id, appointment_id,
        )


async def finalize_call(
    *, call_id: UUID, ended_at: datetime, duration_seconds: int,
) -> None:
    await connection.execute_void(
        """
        UPDATE voice_agent.calls
        SET ended_at = $2, duration_seconds = $3
        WHERE id = $1
        """,
        call_id, ended_at, duration_seconds,
    )


async def insert_call_turn(
    *, call_id: UUID, role: str, text: str, seq: int,
) -> None:
    await connection.execute_void(
        """
        INSERT INTO voice_agent.call_turns (call_id, role, text, seq)
        VALUES ($1, $2, $3, $4)
        """,
        call_id, role, text, seq,
    )


async def insert_callback_memo(
    *, call_id: UUID, shop_id: UUID, customer_id: UUID | None,
    caller_phone: str | None, reason: str, callback_window: str | None,
) -> UUID:
    row = await connection.execute_one(
        """
        INSERT INTO voice_agent.callback_memos
            (call_id, shop_id, customer_id, caller_phone, reason, callback_window)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        call_id, shop_id, customer_id, caller_phone, reason, callback_window,
    )
    return row["id"]


async def list_memos(
    *, shop_id: UUID, status: str | None = "pending", limit: int = 50,
) -> list[dict]:
    if status:
        return await connection.execute(
            """
            SELECT m.*, c.service_brief, c.summary AS call_summary, c.outcome
            FROM voice_agent.callback_memos m
            LEFT JOIN voice_agent.calls c ON c.id = m.call_id
            WHERE m.shop_id = $1 AND m.status = $2
            ORDER BY m.created_at DESC LIMIT $3
            """,
            shop_id, status, limit,
        )
    return await connection.execute(
        """
        SELECT m.*, c.service_brief, c.summary AS call_summary, c.outcome
        FROM voice_agent.callback_memos m
        LEFT JOIN voice_agent.calls c ON c.id = m.call_id
        WHERE m.shop_id = $1
        ORDER BY m.created_at DESC LIMIT $2
        """,
        shop_id, limit,
    )


async def count_pending_memos(*, shop_id: UUID) -> int:
    """Open-escalation count for the Action Center tile."""
    row = await connection.execute_one(
        "SELECT count(*) AS n FROM voice_agent.callback_memos "
        "WHERE shop_id = $1 AND status = 'pending'",
        shop_id,
    )
    return int(row["n"]) if row else 0


async def update_memo_status(
    *, memo_id: UUID, status: str, actioned_by: UUID | None,
) -> bool:
    await connection.execute_void(
        """
        UPDATE voice_agent.callback_memos
        SET status = $2,
            actioned_by = $3,
            actioned_at = CASE WHEN $2 IN ('actioned','dismissed') THEN now() ELSE actioned_at END
        WHERE id = $1
        """,
        memo_id, status, actioned_by,
    )
    return True