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
    brief_row = await execute_one(
        "SELECT service_brief FROM voice_agent.calls WHERE id = $1", call_id,
    )
    return {
        "call": call, "transcript": transcript, "events": events,
        "service_brief": brief_row["service_brief"] if brief_row else None,
    }


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


_OUTCOME_KEYS = ("booked", "rescheduled", "cancelled", "info",
                 "abandoned", "escalated", "failed")


async def get_analytics(
    shop_id: UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> dict:
    where = ["shop_id = $1"]
    params: list = [shop_id]
    if from_dt:
        params.append(from_dt)
        where.append(f"started_at >= ${len(params)}")
    if to_dt:
        params.append(to_dt)
        where.append(f"started_at <= ${len(params)}")
    w = " AND ".join(where)

    totals = await execute_one(
        f"SELECT COUNT(*) AS total, "
        f"COALESCE(AVG(duration_seconds), 0)::int AS avg_dur, "
        f"COUNT(*) FILTER (WHERE outcome = 'failed') AS failed "
        f"FROM voice_agent.calls WHERE {w}",
        *params,
    )
    total = totals["total"] or 0
    failed = totals["failed"] or 0
    failure_rate = (failed / total) if total else 0.0

    by_day_rows = await execute(
        f"SELECT (started_at AT TIME ZONE 'Europe/Rome')::date AS d, COUNT(*) AS c "
        f"FROM voice_agent.calls WHERE {w} GROUP BY d ORDER BY d",
        *params,
    )
    by_day = [{"date": r["d"].isoformat(), "count": r["c"]} for r in by_day_rows]

    outcome_rows = await execute(
        f"SELECT outcome, COUNT(*) AS c FROM voice_agent.calls "
        f"WHERE {w} GROUP BY outcome",
        *params,
    )
    outcome_counts = {k: 0 for k in _OUTCOME_KEYS}
    for row in outcome_rows:
        if row["outcome"] in outcome_counts:
            outcome_counts[row["outcome"]] = row["c"]
    non_failed = total - outcome_counts["failed"]
    conversion = (outcome_counts["booked"] / non_failed) if non_failed else 0.0

    top_services_rows = await execute(
        f"SELECT sid AS service_id, COUNT(*) AS c "
        f"FROM voice_agent.calls c, UNNEST(c.requested_service_ids) AS sid "
        f"WHERE {w} GROUP BY sid ORDER BY c DESC LIMIT 10",
        *params,
    )
    service_ids = [r["service_id"] for r in top_services_rows]
    name_map: dict = {}
    if service_ids:
        for row in await execute(
            "SELECT id, service_name FROM business_app_core.services WHERE id = ANY($1)",
            service_ids,
        ):
            name_map[row["id"]] = row["service_name"]
    top_services = [
        {"service_id": str(r["service_id"]),
         "name": name_map.get(r["service_id"], ""),
         "count": r["c"]}
        for r in top_services_rows
    ]

    top_staff_rows = await execute(
        f"SELECT requested_staff_id AS staff_id, COUNT(*) AS c "
        f"FROM voice_agent.calls WHERE {w} AND requested_staff_id IS NOT NULL "
        f"GROUP BY staff_id ORDER BY c DESC LIMIT 10",
        *params,
    )
    staff_ids = [r["staff_id"] for r in top_staff_rows]
    staff_name_map: dict = {}
    if staff_ids:
        for row in await execute(
            "SELECT id, full_name FROM business_app_core.staff WHERE id = ANY($1)",
            staff_ids,
        ):
            staff_name_map[row["id"]] = row["full_name"]
    top_staff = [
        {"staff_id": str(r["staff_id"]),
         "name": staff_name_map.get(r["staff_id"], ""),
         "count": r["c"]}
        for r in top_staff_rows
    ]

    by_hour_rows = await execute(
        f"SELECT EXTRACT(HOUR FROM started_at AT TIME ZONE 'Europe/Rome')::int AS h, "
        f"COUNT(*) AS c FROM voice_agent.calls WHERE {w} GROUP BY h ORDER BY h",
        *params,
    )
    by_hour = [{"hour": r["h"], "count": r["c"]} for r in by_hour_rows]

    by_dow_rows = await execute(
        f"SELECT EXTRACT(ISODOW FROM started_at AT TIME ZONE 'Europe/Rome')::int AS d, "
        f"COUNT(*) AS c FROM voice_agent.calls WHERE {w} GROUP BY d ORDER BY d",
        *params,
    )
    by_dow = [{"dow": r["d"] - 1, "count": r["c"]} for r in by_dow_rows]  # 0=Monday

    after_hours = await execute_one(
        f"SELECT COUNT(*) AS c FROM voice_agent.calls c "
        f"WHERE {w} AND NOT EXISTS ("
        f"  SELECT 1 FROM business_app_core.staff s "
        f"  JOIN business_app_core.staff_schedules sch ON sch.staff_id = s.id "
        f"  WHERE s.shop_id = $1 AND s.is_active = true "
        f"    AND sch.day_of_week = (EXTRACT(ISODOW FROM c.started_at "
        f"        AT TIME ZONE 'Europe/Rome')::int - 1) "
        f"    AND (c.started_at AT TIME ZONE 'Europe/Rome')::time "
        f"        BETWEEN sch.start_time AND sch.end_time"
        f")",
        *params,
    )
    after_hours_pct = (after_hours["c"] / total) if total else 0.0

    return {
        "volume": {
            "total": total,
            "by_day": by_day,
            "avg_duration_sec": totals["avg_dur"] or 0,
            "failure_rate": failure_rate,
        },
        "outcomes": {**outcome_counts, "conversion_rate": conversion},
        "demand": {
            "top_services": top_services,
            "top_staff": top_staff,
            "by_hour": by_hour,
            "by_dow": by_dow,
            "after_hours_pct": after_hours_pct,
        },
    }
