"""SQL query functions for all Booking Engine operations (PostgreSQL / Neon)."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from booking_engine.db.connection import execute, execute_one, execute_void


class SlotConflictError(Exception):
    """Raised when a booking would overlap an existing appointment."""


async def get_shop(shop_id: UUID) -> dict | None:
    return await execute_one(
        "SELECT * FROM business_app_core.shops WHERE id = $1 AND is_active = true",
        shop_id,
    )


async def list_staff(shop_id: UUID) -> list[dict]:
    return await execute(
        "SELECT id, full_name, role, bio FROM business_app_core.staff "
        "WHERE shop_id = $1 AND is_active = true ORDER BY full_name",
        shop_id,
    )


async def get_staff_services(staff_id: UUID) -> list[dict]:
    return await execute(
        "SELECT s.id, s.service_name, s.duration_minutes, s.price_eur, s.category "
        "FROM business_app_core.services s JOIN business_app_core.staff_services ss ON s.id = ss.service_id "
        "WHERE ss.staff_id = $1 AND s.is_active = true ORDER BY s.service_name",
        staff_id,
    )


async def list_services(shop_id: UUID) -> list[dict]:
    return await execute(
        "SELECT id, service_name, description, duration_minutes, price_eur, category "
        "FROM business_app_core.services WHERE shop_id = $1 AND is_active = true "
        "ORDER BY category, service_name",
        shop_id,
    )


async def find_customers_by_phone(shop_id: UUID, phone: str) -> list[dict]:
    return await execute(
        "SELECT c.id, c.full_name, c.preferred_staff_id, c.notes "
        "FROM business_app_core.customers c JOIN business_app_core.phone_contacts pc ON c.id = pc.customer_id "
        "WHERE c.shop_id = $1 AND pc.phone_number = $2 "
        "ORDER BY pc.last_seen_at DESC",
        shop_id, phone,
    )


async def find_customers_by_name_and_phone(
    shop_id: UUID, name: str, phone: str
) -> list[dict]:
    return await execute(
        "SELECT c.id, c.full_name, c.preferred_staff_id, c.notes "
        "FROM business_app_core.customers c JOIN business_app_core.phone_contacts pc ON c.id = pc.customer_id "
        "WHERE c.shop_id = $1 AND pc.phone_number = $2 "
        "AND LOWER(c.full_name) LIKE LOWER($3) || '%' "
        "ORDER BY pc.last_seen_at DESC",
        shop_id, phone, name,
    )


async def create_customer(
    shop_id: UUID, full_name: str, phone_number: str | None = None,
) -> dict:
    cid = uuid4()
    # source/verified are not decoration: nothing reaches this function except
    # through the booking engine, so the row is by definition something the
    # assistant made rather than something the owner typed. The webapp's
    # anagrafiche badge is driven by `verified = false` ALONE — a row created
    # here without it is indistinguishable from a hand-typed one and no human
    # will ever be prompted to look at it. insert_customer_from_call() has set
    # both since it was written; this path was left on the column defaults
    # (`manual` / `true`) and quietly wasn't.
    await execute_void(
        "INSERT INTO business_app_core.customers "
        "(id, shop_id, full_name, source, verified, created_at) "
        "VALUES ($1, $2, $3, 'voice_agent', false, NOW())",
        cid, shop_id, full_name,
    )
    customer = await execute_one("SELECT * FROM business_app_core.customers WHERE id = $1", cid)
    if phone_number and customer:
        existing = await execute_one(
            "SELECT id FROM business_app_core.phone_contacts WHERE phone_number = $1 AND customer_id = $2",
            phone_number, cid,
        )
        if existing:
            await execute_void(
                "UPDATE business_app_core.phone_contacts SET last_seen_at = NOW() "
                "WHERE phone_number = $1 AND customer_id = $2",
                phone_number, cid,
            )
        else:
            await execute_void(
                "INSERT INTO business_app_core.phone_contacts (id, phone_number, customer_id, last_seen_at) "
                "VALUES ($1, $2, $3, NOW())",
                uuid4(), phone_number, cid,
            )
    return customer


async def upsert_phone_contact(phone: str, customer_id: UUID) -> None:
    await execute_void(
        "INSERT INTO business_app_core.phone_contacts (id, phone_number, customer_id, last_seen_at) "
        "VALUES ($1, $2, $3, NOW()) "
        "ON CONFLICT (phone_number, customer_id) DO UPDATE SET last_seen_at = NOW()",
        uuid4(), phone, customer_id,
    )


async def create_appointment(
    shop_id: UUID,
    customer_id: UUID,
    staff_id: UUID,
    service_ids: list[UUID],
    start_time: datetime,
    notes: str | None = None,
) -> dict:
    svc_rows = await execute(
        "SELECT id, duration_minutes FROM business_app_core.services "
        "WHERE id = ANY($1::uuid[]) AND is_active = true",
        service_ids,
    )
    total_minutes = sum(r["duration_minutes"] for r in svc_rows)
    end_time = start_time + timedelta(minutes=total_minutes)

    # Check for overlapping appointments
    overlap = await execute(
        "SELECT id FROM business_app_core.appointments "
        "WHERE staff_id = $1 AND status NOT IN ('cancelled', 'no_show') "
        "AND start_time < $2 AND end_time > $3",
        staff_id, end_time, start_time,
    )
    if overlap:
        raise SlotConflictError("Time slot conflicts with existing appointment")

    appt_id = uuid4()
    await execute_void(
        "INSERT INTO business_app_core.appointments (id, shop_id, customer_id, staff_id, start_time, end_time, status, notes, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'scheduled', $7, NOW())",
        appt_id, shop_id, customer_id, staff_id, start_time, end_time, notes,
    )

    for svc in svc_rows:
        # No price column here: services.price_eur is the single source of a
        # service's price (webapp migration 71). Readers join for it.
        await execute_void(
            "INSERT INTO business_app_core.appointment_services (appointment_id, service_id, duration_minutes) "
            "VALUES ($1, $2, $3)",
            appt_id, svc["id"], svc["duration_minutes"],
        )

    return await execute_one("SELECT * FROM business_app_core.appointments WHERE id = $1", appt_id)


async def list_appointments(
    shop_id: UUID,
    customer_id: UUID | None = None,
    status: str | None = None,
) -> list[dict]:
    conditions = ["a.shop_id = $1"]
    args: list = [shop_id]
    idx = 2

    if customer_id:
        conditions.append(f"a.customer_id = ${idx}")
        args.append(customer_id)
        idx += 1
    if status:
        conditions.append(f"a.status = ${idx}")
        args.append(status)
        idx += 1

    where = " AND ".join(conditions)
    rows = await execute(
        f"SELECT a.*, st.full_name AS staff_name "
        f"FROM business_app_core.appointments a JOIN business_app_core.staff st ON a.staff_id = st.id "
        f"WHERE {where} ORDER BY a.start_time",
        *args,
    )

    for row in rows:
        svcs = await execute(
            "SELECT aps.service_id, s.service_name, aps.duration_minutes, s.price_eur "
            "FROM business_app_core.appointment_services aps JOIN business_app_core.services s ON aps.service_id = s.id "
            "WHERE aps.appointment_id = $1",
            row["id"],
        )
        row["services"] = svcs

    return rows


async def cancel_appointment(shop_id: UUID, appointment_id: UUID) -> dict | None:
    existing = await execute_one(
        "SELECT * FROM business_app_core.appointments WHERE id = $1 AND shop_id = $2 AND status IN ('scheduled', 'confirmed')",
        appointment_id, shop_id,
    )
    if not existing:
        return None
    await execute_void(
        "UPDATE business_app_core.appointments SET status = 'cancelled' WHERE id = $1",
        appointment_id,
    )
    return await execute_one("SELECT * FROM business_app_core.appointments WHERE id = $1", appointment_id)


async def reschedule_appointment(
    shop_id: UUID,
    appointment_id: UUID,
    new_start_time: datetime,
    new_staff_id: UUID | None = None,
) -> dict | None:
    current = await execute_one(
        "SELECT * FROM business_app_core.appointments WHERE id = $1 AND shop_id = $2 AND status IN ('scheduled', 'confirmed')",
        appointment_id, shop_id,
    )
    if not current:
        return None

    c_start = current["start_time"]
    c_end = current["end_time"]
    if isinstance(c_start, str):
        c_start = datetime.fromisoformat(c_start)
        c_end = datetime.fromisoformat(c_end)
    duration = c_end - c_start
    new_end = new_start_time + duration
    staff = new_staff_id if new_staff_id else current["staff_id"]

    # Cancel old
    await execute_void("UPDATE business_app_core.appointments SET status = 'cancelled' WHERE id = $1", appointment_id)

    # Create new
    new_id = uuid4()
    await execute_void(
        "INSERT INTO business_app_core.appointments (id, shop_id, customer_id, staff_id, start_time, end_time, status, notes, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'scheduled', $7, NOW())",
        new_id, shop_id, current["customer_id"], staff,
        new_start_time, new_end, current.get("notes"),
    )

    # Copy services
    old_svcs = await execute(
        "SELECT service_id, duration_minutes FROM business_app_core.appointment_services WHERE appointment_id = $1",
        appointment_id,
    )
    for svc in old_svcs:
        await execute_void(
            "INSERT INTO business_app_core.appointment_services (appointment_id, service_id, duration_minutes) "
            "VALUES ($1, $2, $3)",
            new_id, svc["service_id"], svc["duration_minutes"],
        )

    return await execute_one("SELECT * FROM business_app_core.appointments WHERE id = $1", new_id)
