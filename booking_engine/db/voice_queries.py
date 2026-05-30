"""SQL query functions for voice_agent schema and shops.voice/language columns."""
from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

from booking_engine.db.connection import execute, execute_one, execute_void


_VOICE_CONFIG_FIELDS = (
    "welcome_message", "tone_instructions", "personality", "special_instructions",
    "voice", "language", "is_active",
)
_ALLOWED_UPDATE_FIELDS = set(_VOICE_CONFIG_FIELDS)


async def get_voice_config(shop_id: UUID) -> dict | None:
    cols = ", ".join(_VOICE_CONFIG_FIELDS)
    return await execute_one(
        f"SELECT {cols} FROM business_app_core.shops WHERE id = $1",
        shop_id,
    )


async def update_voice_config(shop_id: UUID, patch: dict) -> dict | None:
    fields = [(k, v) for k, v in patch.items() if k in _ALLOWED_UPDATE_FIELDS]
    if not fields:
        return await get_voice_config(shop_id)
    set_clause = ", ".join(f"{k} = ${i+2}" for i, (k, _) in enumerate(fields))
    values = [v for _, v in fields]
    await execute_void(
        f"UPDATE business_app_core.shops SET {set_clause} WHERE id = $1",
        shop_id, *values,
    )
    return await get_voice_config(shop_id)


_CALL_SUMMARY_COLS = (
    "id, caller_number, customer_id, customer_match, started_at, ended_at, "
    "duration_seconds, outcome, summary, appointment_id"
)


def _encode_cursor(started_at: datetime, call_id: UUID) -> str:
    raw = json.dumps({"t": started_at.isoformat(), "id": str(call_id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    obj = json.loads(raw)
    return datetime.fromisoformat(obj["t"]), UUID(obj["id"])


async def list_calls(
    shop_id: UUID,
    filters: dict,
    cursor: str | None,
    limit: int = 20,
) -> dict:
    """Filters: { outcome: list[str], from: datetime, to: datetime, q: str }."""
    limit = max(1, min(limit, 100))
    where = ["shop_id = $1"]
    params: list = [shop_id]

    outcomes = filters.get("outcome") or []
    if outcomes:
        params.append(outcomes)
        where.append(f"outcome = ANY(${len(params)})")
    if filters.get("from"):
        params.append(filters["from"])
        where.append(f"started_at >= ${len(params)}")
    if filters.get("to"):
        params.append(filters["to"])
        where.append(f"started_at <= ${len(params)}")
    if filters.get("q"):
        params.append(f"%{filters['q']}%")
        where.append(f"(caller_number ILIKE ${len(params)} OR summary ILIKE ${len(params)})")
    if cursor:
        cursor_dt, cursor_id = _decode_cursor(cursor)
        params.append(cursor_dt)
        params.append(cursor_id)
        where.append(
            f"(started_at, id) < (${len(params)-1}, ${len(params)})"
        )

    sql = (
        f"SELECT {_CALL_SUMMARY_COLS} FROM voice_agent.calls "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY started_at DESC, id DESC LIMIT {limit + 1}"
    )
    rows = await execute(sql, *params)
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(last["started_at"], last["id"])
        rows = rows[:limit]
    return {"items": rows, "next_cursor": next_cursor}


async def get_call_detail(shop_id: UUID, call_id: UUID) -> dict | None:
    call = await execute_one(
        f"SELECT {_CALL_SUMMARY_COLS} FROM voice_agent.calls "
        "WHERE shop_id = $1 AND id = $2",
        shop_id, call_id,
    )
    if not call:
        return None
    transcript = await execute(
        "SELECT turn_index, role, text, at FROM voice_agent.call_transcripts "
        "WHERE call_id = $1 ORDER BY turn_index",
        call_id,
    )
    events = await execute(
        "SELECT at, type, payload FROM voice_agent.call_events "
        "WHERE call_id = $1 ORDER BY at",
        call_id,
    )
    return {"call": call, "transcript": transcript, "events": events}


async def link_customer(shop_id: UUID, call_id: UUID, customer_id: UUID) -> dict | None:
    await execute_void(
        "UPDATE voice_agent.calls SET customer_id = $3, customer_match = 'existing' "
        "WHERE shop_id = $1 AND id = $2",
        shop_id, call_id, customer_id,
    )
    return await execute_one(
        f"SELECT {_CALL_SUMMARY_COLS} FROM voice_agent.calls "
        "WHERE shop_id = $1 AND id = $2",
        shop_id, call_id,
    )
