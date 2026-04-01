"""SQL query functions for all Booking Engine operations (PostgreSQL / Neon)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from booking_engine.db.connection import execute, execute_one, execute_void

_ROME = ZoneInfo("Europe/Rome")


class SlotConflictError(Exception):
    """Raised when a booking would overlap an existing appointment."""


async def get_shop(shop_id: UUID) -> dict | None:
    return await execute_one(
        "SELECT * FROM shops WHERE id = $1 AND is_active = true",
        shop_id,
    )


async def list_staff(shop_id: UUID) -> list[dict]:
    return await execute(
        "SELECT id, full_name, role, bio FROM staff "
        "WHERE shop_id = $1 AND is_active = true ORDER BY full_name",
        shop_id,
    )


async def get_staff_services(staff_id: UUID) -> list[dict]:
    return await execute(
        "SELECT s.id, s.service_name, s.duration_minutes, s.price_eur, s.category "
        "FROM services s JOIN staff_services ss ON s.id = ss.service_id "
        "WHERE ss.staff_id = $1 AND s.is_active = true ORDER BY s.service_name",
        staff_id,
    )


async def list_services(shop_id: UUID) -> list[dict]:
    return await execute(
        "SELECT id, service_name, description, duration_minutes, price_eur, category "
        "FROM services WHERE shop_id = $1 AND is_active = true "
        "ORDER BY category, service_name",
        shop_id,
    )


async def find_customers_by_phone(shop_id: UUID, phone: str) -> list[dict]:
    return await execute(
        "SELECT c.id, c.full_name, c.preferred_staff_id, c.notes "
        "FROM customers c JOIN phone_contacts pc ON c.id = pc.customer_id "
        "WHERE c.shop_id = $1 AND pc.phone_number = $2 "
        "ORDER BY pc.last_seen_at DESC",
        shop_id, phone,
    )


async def find_customers_by_name_and_phone(
    shop_id: UUID, name: str, phone: str
) -> list[dict]:
    return await execute(
        "SELECT c.id, c.full_name, c.preferred_staff_id, c.notes "
        "FROM customers c JOIN phone_contacts pc ON c.id = pc.customer_id "
        "WHERE c.shop_id = $1 AND pc.phone_number = $2 "
        "AND LOWER(c.full_name) LIKE LOWER($3) || '%' "
        "ORDER BY pc.last_seen_at DESC",
        shop_id, phone, name,
    )


async def create_customer(
    shop_id: UUID, full_name: str, phone_number: str | None = None,
) -> dict:
    cid = uuid4()
    await execute_void(
        "INSERT INTO customers (id, shop_id, full_name, created_at) "
        "VALUES ($1, $2, $3, NOW())",
        cid, shop_id, full_name,
    )
    customer = await execute_one("SELECT * FROM customers WHERE id = $1", cid)
    if phone_number and customer:
        existing = await execute_one(
            "SELECT id FROM phone_contacts WHERE phone_number = $1 AND customer_id = $2",
            phone_number, cid,
        )
        if existing:
            await execute_void(
                "UPDATE phone_contacts SET last_seen_at = NOW() "
                "WHERE phone_number = $1 AND customer_id = $2",
                phone_number, cid,
            )
        else:
            await execute_void(
                "INSERT INTO phone_contacts (id, phone_number, customer_id, last_seen_at) "
                "VALUES ($1, $2, $3, NOW())",
                uuid4(), phone_number, cid,
            )
    return customer


async def upsert_phone_contact(phone: str, customer_id: UUID) -> None:
    await execute_void(
        "INSERT INTO phone_contacts (id, phone_number, customer_id, last_seen_at) "
        "VALUES ($1, $2, $3, NOW()) "
        "ON CONFLICT (phone_number, customer_id) DO UPDATE SET last_seen_at = NOW()",
        uuid4(), phone, customer_id,
    )


async def get_available_slots(
    shop_id: UUID,
    service_ids: list[UUID],
    start_date: date,
    end_date: date,
    staff_id: UUID | None = None,
) -> list[dict]:
    """Compute available booking slots (Python logic + SQL lookups)."""

    # Get total duration for requested services
    svc_rows = await execute(
        "SELECT SUM(duration_minutes) AS total FROM services "
        "WHERE id = ANY($1::uuid[]) AND is_active = true",
        service_ids,
    )
    total_minutes = int(svc_rows[0]["total"]) if svc_rows and svc_rows[0]["total"] else 0
    if total_minutes == 0:
        return []

    # Find eligible staff (who can do ALL requested services)
    num_services = len(service_ids)
    if staff_id:
        eligible = await execute(
            "SELECT st.id AS staff_id, st.full_name AS staff_name "
            "FROM staff st "
            "WHERE st.shop_id = $1 AND st.is_active = true AND st.id = $2 "
            "AND (SELECT COUNT(DISTINCT ss.service_id) FROM staff_services ss "
            "     WHERE ss.staff_id = st.id AND ss.service_id = ANY($3::uuid[])) = $4",
            shop_id, staff_id, service_ids, num_services,
        )
    else:
        eligible = await execute(
            "SELECT st.id AS staff_id, st.full_name AS staff_name "
            "FROM staff st "
            "WHERE st.shop_id = $1 AND st.is_active = true "
            "AND (SELECT COUNT(DISTINCT ss.service_id) FROM staff_services ss "
            "     WHERE ss.staff_id = st.id AND ss.service_id = ANY($2::uuid[])) = $3",
            shop_id, service_ids, num_services,
        )
    if not eligible:
        return []

    # Generate candidate slots per staff per day
    slots = []
    current = start_date
    while current <= end_date:
        dow = current.weekday()
        for staff_row in eligible:
            sid = staff_row["staff_id"]
            sname = staff_row["staff_name"]
            scheds = await execute(
                "SELECT start_time, end_time FROM staff_schedules "
                "WHERE staff_id = $1 AND day_of_week = $2",
                sid, dow,
            )
            for sched in scheds:
                st_parts = str(sched["start_time"]).split(":")
                et_parts = str(sched["end_time"]).split(":")
                win_start = datetime.combine(current, time(int(st_parts[0]), int(st_parts[1])), tzinfo=_ROME)
                win_end = datetime.combine(current, time(int(et_parts[0]), int(et_parts[1])), tzinfo=_ROME)

                slot_start = win_start
                while slot_start + timedelta(minutes=total_minutes) <= win_end:
                    slot_end = slot_start + timedelta(minutes=total_minutes)
                    slots.append({
                        "staff_id": sid, "staff_name": sname,
                        "slot_start": slot_start, "slot_end": slot_end,
                    })
                    slot_start += timedelta(minutes=30)
        current += timedelta(days=1)

    if not slots:
        return []

    # Filter out slots that overlap existing appointments
    staff_ids = [s["staff_id"] for s in eligible]
    from_ts = datetime.combine(start_date, time(0, 0), tzinfo=_ROME)
    to_ts = datetime.combine(end_date, time(23, 59), tzinfo=_ROME)
    existing = await execute(
        "SELECT staff_id, start_time, end_time FROM appointments "
        "WHERE staff_id = ANY($1::uuid[]) "
        "AND status NOT IN ('cancelled', 'no_show') "
        "AND start_time < $2 AND end_time > $3",
        staff_ids, to_ts, from_ts,
    )

    def overlaps(slot, appt):
        a_start = appt["start_time"] if isinstance(appt["start_time"], datetime) else datetime.fromisoformat(str(appt["start_time"]))
        a_end = appt["end_time"] if isinstance(appt["end_time"], datetime) else datetime.fromisoformat(str(appt["end_time"]))
        s_start = slot["slot_start"]
        s_end = slot["slot_end"]
        if a_start.tzinfo is None:
            a_start = a_start.replace(tzinfo=_ROME)
            a_end = a_end.replace(tzinfo=_ROME)
        return s_start < a_end and s_end > a_start

    available = []
    for slot in slots:
        blocked = any(
            slot["staff_id"] == appt["staff_id"] and overlaps(slot, appt)
            for appt in existing
        )
        if not blocked:
            available.append(slot)

    available.sort(key=lambda s: (s["slot_start"], s["staff_name"]))
    return available


async def create_appointment(
    shop_id: UUID,
    customer_id: UUID,
    staff_id: UUID,
    service_ids: list[UUID],
    start_time: datetime,
    notes: str | None = None,
) -> dict:
    svc_rows = await execute(
        "SELECT id, duration_minutes, price_eur FROM services "
        "WHERE id = ANY($1::uuid[]) AND is_active = true",
        service_ids,
    )
    total_minutes = sum(r["duration_minutes"] for r in svc_rows)
    end_time = start_time + timedelta(minutes=total_minutes)

    # Check for overlapping appointments
    overlap = await execute(
        "SELECT id FROM appointments "
        "WHERE staff_id = $1 AND status NOT IN ('cancelled', 'no_show') "
        "AND start_time < $2 AND end_time > $3",
        staff_id, end_time, start_time,
    )
    if overlap:
        raise SlotConflictError("Time slot conflicts with existing appointment")

    appt_id = uuid4()
    await execute_void(
        "INSERT INTO appointments (id, shop_id, customer_id, staff_id, start_time, end_time, status, notes, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'scheduled', $7, NOW())",
        appt_id, shop_id, customer_id, staff_id, start_time, end_time, notes,
    )

    for svc in svc_rows:
        await execute_void(
            "INSERT INTO appointment_services (appointment_id, service_id, duration_minutes, price_eur) "
            "VALUES ($1, $2, $3, $4)",
            appt_id, svc["id"], svc["duration_minutes"],
            float(svc["price_eur"]) if svc["price_eur"] else None,
        )

    return await execute_one("SELECT * FROM appointments WHERE id = $1", appt_id)


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
        f"FROM appointments a JOIN staff st ON a.staff_id = st.id "
        f"WHERE {where} ORDER BY a.start_time",
        *args,
    )

    for row in rows:
        svcs = await execute(
            "SELECT aps.service_id, s.service_name, aps.duration_minutes, aps.price_eur "
            "FROM appointment_services aps JOIN services s ON aps.service_id = s.id "
            "WHERE aps.appointment_id = $1",
            row["id"],
        )
        row["services"] = svcs

    return rows


async def cancel_appointment(shop_id: UUID, appointment_id: UUID) -> dict | None:
    existing = await execute_one(
        "SELECT * FROM appointments WHERE id = $1 AND shop_id = $2 AND status IN ('scheduled', 'confirmed')",
        appointment_id, shop_id,
    )
    if not existing:
        return None
    await execute_void(
        "UPDATE appointments SET status = 'cancelled' WHERE id = $1",
        appointment_id,
    )
    return await execute_one("SELECT * FROM appointments WHERE id = $1", appointment_id)


async def reschedule_appointment(
    shop_id: UUID,
    appointment_id: UUID,
    new_start_time: datetime,
    new_staff_id: UUID | None = None,
) -> dict | None:
    current = await execute_one(
        "SELECT * FROM appointments WHERE id = $1 AND shop_id = $2 AND status IN ('scheduled', 'confirmed')",
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
    await execute_void("UPDATE appointments SET status = 'cancelled' WHERE id = $1", appointment_id)

    # Create new
    new_id = uuid4()
    await execute_void(
        "INSERT INTO appointments (id, shop_id, customer_id, staff_id, start_time, end_time, status, notes, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'scheduled', $7, NOW())",
        new_id, shop_id, current["customer_id"], staff,
        new_start_time, new_end, current.get("notes"),
    )

    # Copy services
    old_svcs = await execute(
        "SELECT service_id, duration_minutes, price_eur FROM appointment_services WHERE appointment_id = $1",
        appointment_id,
    )
    for svc in old_svcs:
        await execute_void(
            "INSERT INTO appointment_services (appointment_id, service_id, duration_minutes, price_eur) "
            "VALUES ($1, $2, $3, $4)",
            new_id, svc["service_id"], svc["duration_minutes"],
            float(svc["price_eur"]) if svc["price_eur"] else None,
        )

    return await execute_one("SELECT * FROM appointments WHERE id = $1", new_id)
