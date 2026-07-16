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


async def list_services(*, shop_id: UUID, filter_q: str | None) -> list[dict]:
    # Ground-truth business_app_core.services; alias to the keys the tool route
    # expects (name/duration_min/price_cents). price stored as euros -> cents.
    if filter_q:
        return await connection.execute(
            """
            SELECT id, service_name AS name, duration_minutes AS duration_min,
                   (price_eur * 100)::int AS price_cents
            FROM business_app_core.services
            WHERE shop_id = $1 AND is_active = true
              AND service_name ILIKE '%' || $2 || '%'
            ORDER BY service_name
            LIMIT 20
            """,
            shop_id, filter_q,
        )
    return await connection.execute(
        """
        SELECT id, service_name AS name, duration_minutes AS duration_min,
               (price_eur * 100)::int AS price_cents
        FROM business_app_core.services
        WHERE shop_id = $1 AND is_active = true
        ORDER BY service_name
        """,
        shop_id,
    )


async def list_staff_for_service(*, shop_id: UUID, service_id: UUID) -> list[dict]:
    return await connection.execute(
        """
        SELECT s.id, s.full_name AS name
        FROM business_app_core.staff s
        JOIN business_app_core.staff_services ss ON ss.staff_id = s.id
        WHERE s.shop_id = $1 AND ss.service_id = $2 AND s.is_active = true
        ORDER BY s.full_name
        """,
        shop_id, service_id,
    )


async def find_availability(
    *, shop_id: UUID, service_id: UUID,
    preferred_when: datetime | None,
    staff_id: UUID | None,
    max_results: int,
) -> list[dict]:
    """Open slots for the service — delegates to the ground-truth booking layer."""
    from datetime import datetime, timedelta

    from booking_engine.db import queries

    start_date = (preferred_when or datetime.utcnow()).date()
    end_date = start_date + timedelta(days=14)
    slots = await queries.get_available_slots(
        shop_id=shop_id, service_ids=[service_id],
        start_date=start_date, end_date=end_date, staff_id=staff_id,
    )
    return slots[:max_results]


async def insert_booking_locked(
    *, shop_id: UUID, customer_id: UUID, service_id: UUID,
    slot_start: datetime, staff_id: UUID,
) -> dict:
    """Create the appointment via the ground-truth layer. Raises on conflict.

    ponytail: relies on create_appointment's overlap check (no advisory lock).
    Add a lock only if concurrent voice bookings for the same staff+slot become
    a real problem.
    """
    from booking_engine.db import queries

    try:
        row = await queries.create_appointment(
            shop_id=shop_id, customer_id=customer_id, staff_id=staff_id,
            service_ids=[service_id], start_time=slot_start,
        )
    except queries.SlotConflictError:
        raise RuntimeError("slot_taken")
    return {
        "id": row["id"],
        "slot_start": row["start_time"],
        "slot_end": row["end_time"],
        "staff_id": row["staff_id"],
        "confirmation_status": row.get("confirmation_status") or "confirmed",
    }


async def attach_booking_to_call(*, call_id: UUID, appointment_id: UUID) -> None:
    await connection.execute_void(
        "UPDATE voice_agent.calls SET created_booking_id = $2 WHERE id = $1",
        call_id, appointment_id,
    )
    await connection.execute_void(
        "UPDATE business_app_core.appointments SET voice_call_id = $1 WHERE id = $2",
        call_id, appointment_id,
    )


async def get_next_booking_for_customer(
    *, shop_id: UUID, customer_id: UUID,
) -> dict | None:
    return await connection.execute_one(
        """
        SELECT a.id, a.start_time, a.end_time, a.staff_id, a.status,
               (SELECT s.service_name FROM business_app_core.services s
                JOIN business_app_core.appointment_services aps ON aps.service_id = s.id
                WHERE aps.appointment_id = a.id LIMIT 1) AS service_name
        FROM business_app_core.appointments a
        WHERE a.shop_id = $1 AND a.customer_id = $2
          AND a.start_time > now() AND a.status NOT IN ('cancelled')
        ORDER BY a.start_time
        LIMIT 1
        """,
        shop_id, customer_id,
    )


async def modify_appointment(
    *, shop_id: UUID, appointment_id: UUID,
    new_slot_start: datetime | None,
    new_service_id: UUID | None = None,
) -> bool:
    """Reschedule via the ground-truth layer (cancels + recreates, copies services).

    ponytail: only time changes are supported for now; new_service_id is ignored.
    Wire a service swap through appointment_services if the product needs it.
    """
    if new_slot_start is None:
        return False
    from booking_engine.db import queries

    result = await queries.reschedule_appointment(
        shop_id=shop_id, appointment_id=appointment_id,
        new_start_time=new_slot_start,
    )
    return result is not None


async def service_belongs_to_shop(*, shop_id: UUID, service_id: UUID) -> bool:
    """True if the service exists in this shop's active catalog."""
    row = await connection.execute_one(
        "SELECT 1 AS ok FROM business_app_core.services "
        "WHERE id = $1 AND shop_id = $2 AND is_active = true",
        service_id, shop_id,
    )
    return row is not None


async def get_appointment_owner(*, appointment_id: UUID) -> dict | None:
    """Return {shop_id, customer_id, phones[]} for an appointment, or None.

    Used to authorize reschedule/cancel: the caller must own this booking.
    """
    return await connection.execute_one(
        """
        SELECT a.shop_id, a.customer_id, a.start_time AS start_at,
               coalesce(
                 array_agg(pc.phone_number) FILTER (WHERE pc.phone_number IS NOT NULL),
                 '{}'
               ) AS phones
        FROM business_app_core.appointments a
        LEFT JOIN business_app_core.phone_contacts pc
               ON pc.customer_id = a.customer_id
        WHERE a.id = $1
        GROUP BY a.shop_id, a.customer_id, a.start_time
        """,
        appointment_id,
    )


async def cancel_appointment(*, shop_id: UUID, appointment_id: UUID) -> bool:
    """Cancel via the ground-truth layer (also guards shop ownership + status)."""
    from booking_engine.db import queries

    result = await queries.cancel_appointment(
        shop_id=shop_id, appointment_id=appointment_id,
    )
    return result is not None


async def log_auth_event(
    *, call_id: UUID, customer_id: UUID | None,
    verification_question: str, caller_answer_excerpt: str, passed: bool,
) -> None:
    await connection.execute_void(
        """
        INSERT INTO voice_agent.auth_events
            (call_id, customer_id, verification_question,
             caller_answer_excerpt, passed)
        VALUES ($1, $2, $3, $4, $5)
        """,
        call_id, customer_id, verification_question, caller_answer_excerpt, passed,
    )