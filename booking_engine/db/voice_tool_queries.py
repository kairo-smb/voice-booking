"""DB access for voice agent tools — customers, services, appointments."""
from __future__ import annotations

from uuid import UUID

from booking_engine.db import connection


async def find_customers_by_phone(*, shop_id: UUID, phone_digits: str) -> list[dict]:
    """Find customers whose phone normalizes to the same digits."""
    if not phone_digits:
        return []
    return await connection.execute(
        """
        SELECT id, first_name, last_name, last_visit_at,
               preferred_staff_id, notes_tags, verified
        FROM business_app_core.customers
        WHERE shop_id = $1 AND phone_normalized = $2
        LIMIT 5
        """,
        shop_id, phone_digits,
    )


async def insert_customer_from_call(
    *,
    shop_id: UUID,
    phone: str,
    first_name: str,
    last_name: str | None,
    phone_verified: bool,
    created_by_call_id: UUID,
) -> UUID:
    row = await connection.execute_one(
        """
        INSERT INTO business_app_core.customers
            (shop_id, phone, first_name, last_name,
             source, created_by_call_id, verified, phone_verified,
             notes_tags, created_at)
        VALUES
            ($1, $2, $3, $4,
             'voice_agent', $5, false, $6,
             ARRAY['nuovo da chiamata vocale']::text[], now())
        RETURNING id
        """,
        shop_id, phone, first_name, last_name,
        created_by_call_id, phone_verified,
    )
    return row["id"]


async def update_customer_field(
    *, customer_id: UUID, field: str, value: str
) -> bool:
    allowed = {"last_name", "email", "notes_tags"}
    if field not in allowed:
        return False
    if field == "notes_tags":
        # append a tag rather than replace
        sql = (
            "UPDATE business_app_core.customers "
            "SET notes_tags = array_append(coalesce(notes_tags, ARRAY[]::text[]), $2), "
            "    updated_at = now() "
            "WHERE id = $1"
        )
    else:
        sql = (
            f"UPDATE business_app_core.customers "
            f"SET {field} = $2, updated_at = now() WHERE id = $1"
        )
    await connection.execute_void(sql, customer_id, value)
    return True


async def attach_customer_to_call(
    *, call_id: UUID, created_customer_id: UUID | None = None,
    matched_customer_id: UUID | None = None,
) -> None:
    sets = []
    args: list = [call_id]
    if created_customer_id is not None:
        args.append(created_customer_id)
        sets.append(f"created_customer_id = ${len(args)}")
    if matched_customer_id is not None:
        args.append(matched_customer_id)
        sets.append(f"matched_customer_id = ${len(args)}")
    if not sets:
        return
    sql = f"UPDATE voice_agent.calls SET {', '.join(sets)} WHERE id = $1"
    await connection.execute_void(sql, *args)