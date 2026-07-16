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
    """Return open slots for the service. Naive search — refines later."""
    from datetime import datetime, timedelta

    horizon_start = preferred_when or datetime.utcnow()
    horizon_end = horizon_start + timedelta(days=14)
    return await connection.execute(
        """
        WITH service AS (
            SELECT duration_min FROM business_app_core.services WHERE id = $2
        ),
        candidates AS (
            SELECT sched.staff_id,
                   (sched.day_date + sched.start_time)::timestamptz AS slot_start,
                   (sched.day_date + sched.start_time + ((SELECT duration_min FROM service) || ' minutes')::interval)::timestamptz AS slot_end
            FROM business_app_core.staff_schedules sched
            JOIN business_app_core.staff_services ss
              ON ss.staff_id = sched.staff_id AND ss.service_id = $2
            WHERE sched.shop_id = $1
              AND sched.day_date BETWEEN $3::date AND $4::date
              AND ($5::uuid IS NULL OR sched.staff_id = $5)
        ),
        not_booked AS (
            SELECT c.*, (SELECT first_name || ' ' || coalesce(last_name,'')
                         FROM business_app_core.staff_users WHERE id = c.staff_id) AS staff_name
            FROM candidates c
            WHERE NOT EXISTS (
                SELECT 1 FROM business_app_core.appointments a
                WHERE a.shop_id = $1 AND a.staff_id = c.staff_id
                  AND tstzrange(a.start_at, a.end_at) && tstzrange(c.slot_start, c.slot_end)
                  AND a.status NOT IN ('cancelled')
            )
        )
        SELECT * FROM not_booked ORDER BY slot_start LIMIT $6
        """,
        shop_id, service_id, horizon_start, horizon_end, staff_id, max_results,
    )


async def insert_booking_locked(
    *, shop_id: UUID, customer_id: UUID, service_id: UUID,
    slot_start: datetime, staff_id: UUID,
) -> dict:
    """Insert booking with advisory lock to prevent race conditions. Raises on conflict."""
    pool = connection._get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            bucket = int(slot_start.timestamp() // 900)
            lock_key = f"{staff_id}|{bucket}"
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", lock_key,
            )
            taken = await conn.fetchval(
                """
                SELECT EXISTS (
                  SELECT 1 FROM business_app_core.appointments
                  WHERE shop_id = $1 AND staff_id = $2
                    AND start_at = $3 AND status NOT IN ('cancelled')
                )
                """,
                shop_id, staff_id, slot_start,
            )
            if taken:
                raise RuntimeError("slot_taken")
            row = await conn.fetchrow(
                """
                INSERT INTO business_app_core.appointments
                    (shop_id, customer_id, staff_id, start_at, end_at,
                     status, source, confirmation_status)
                SELECT $1, $2, $3, $4,
                       $4 + (sv.duration_min || ' minutes')::interval,
                       'confirmed', 'voice_agent', 'confirmed'
                FROM business_app_core.services sv
                WHERE sv.id = $5
                RETURNING id, start_at AS slot_start, end_at AS slot_end,
                          staff_id, confirmation_status
                """,
                shop_id, customer_id, staff_id, slot_start, service_id,
            )
            await conn.execute(
                """
                INSERT INTO business_app_core.appointment_services
                    (appointment_id, service_id)
                VALUES ($1, $2)
                """,
                row["id"], service_id,
            )
            return dict(row)


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
        SELECT a.id, a.start_at, a.end_at, a.staff_id, a.status,
               (SELECT name FROM business_app_core.services s
                JOIN business_app_core.appointment_services aps ON aps.service_id = s.id
                WHERE aps.appointment_id = a.id LIMIT 1) AS service_name
        FROM business_app_core.appointments a
        WHERE a.shop_id = $1 AND a.customer_id = $2
          AND a.start_at > now() AND a.status NOT IN ('cancelled')
        ORDER BY a.start_at
        LIMIT 1
        """,
        shop_id, customer_id,
    )


async def modify_appointment(
    *, appointment_id: UUID, new_slot_start: datetime | None,
    new_service_id: UUID | None,
) -> bool:
    from datetime import datetime

    sets = []
    args: list = [appointment_id]
    if new_slot_start is not None:
        args.append(new_slot_start)
        sets.append(f"start_at = ${len(args)}")
        sets.append(
            f"end_at = ${len(args)} + (end_at - start_at)"
        )
    if not sets:
        return False
    sql = (
        f"UPDATE business_app_core.appointments "
        f"SET {', '.join(sets)}, updated_at = now() WHERE id = $1"
    )
    await connection.execute_void(sql, *args)
    if new_service_id is not None:
        await connection.execute_void(
            """
            UPDATE business_app_core.appointment_services
            SET service_id = $2 WHERE appointment_id = $1
            """,
            appointment_id, new_service_id,
        )
    return True


async def service_belongs_to_shop(*, shop_id: UUID, service_id: UUID) -> bool:
    """True if the service exists in this shop's active catalog."""
    row = await connection.execute_one(
        "SELECT 1 AS ok FROM business_app_core.services "
        "WHERE id = $1 AND shop_id = $2 AND active = true",
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


async def cancel_appointment(*, appointment_id: UUID) -> bool:
    await connection.execute_void(
        """
        UPDATE business_app_core.appointments
        SET status = 'cancelled', updated_at = now() WHERE id = $1
        """,
        appointment_id,
    )
    return True


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