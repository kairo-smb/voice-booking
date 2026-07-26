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