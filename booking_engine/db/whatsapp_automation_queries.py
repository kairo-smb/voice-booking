"""SQL for the owner-configured automation rules and their due-work queries.

Everything here is additive and scoped by shop_id. The two due-work queries
read `business_app_core.appointments` (the webapp's schema, shared across all
three repos) and return exactly the four facts the UTILITY template variables
need — name, salon, appointment time, services — plus the customer's phone for
the send path and the appointment id for the idempotency log.

Both due-work queries exclude anything already in `whatsapp.automation_sends`,
keyed on (rule_key, appointment_id). That PK is what makes the hourly tick safe
to re-run: a tick that crashes after sending but before recording re-sends once,
which is recoverable, while recording-first would silently drop a message
forever.
"""
from __future__ import annotations

import json
from uuid import UUID

from booking_engine.db.connection import execute, execute_one, execute_void


# ------------------------------------------------------------------- rules

def _parse_rule(row: dict) -> dict:
    """asyncpg hands jsonb back as text — parse the params back to a dict.

    Same boundary guard as whatsapp_queries' jsonb handling: a `params` that
    stays a string reaches the API layer as JSON-in-a-string, and the webapp
    would render it raw.
    """
    out = dict(row)
    out["params"] = json.loads(out["params"]) if out.get("params") else {}
    return out


async def get_rules(shop_id: UUID) -> list[dict]:
    """Both rules for a shop, whether or not they have ever been configured.

    Absent rows mean "off" — a shop that has never opened the automations tile
    sends nothing, which is the correct default for a feature that messages
    customers by itself.
    """
    rows = await execute(
        """
        SELECT * FROM whatsapp.automation_rules
        WHERE shop_id = $1
        ORDER BY rule_key
        """,
        shop_id,
    )
    return [_parse_rule(row) for row in rows]


async def list_enabled_rules() -> list[dict]:
    """Every enabled rule across all shops, for the hourly tick."""
    rows = await execute(
        """
        SELECT * FROM whatsapp.automation_rules
        WHERE enabled = true
        ORDER BY shop_id, rule_key
        """
    )
    return [_parse_rule(row) for row in rows]


async def upsert_rule(
    *, shop_id: UUID, rule_key: str, enabled: bool, params: dict,
    weekly_cap: int,
) -> dict:
    """Create or update one rule. Additive and idempotent, like every write here."""
    row = await execute_one(
        """
        INSERT INTO whatsapp.automation_rules
            (shop_id, rule_key, enabled, params, weekly_cap)
        VALUES ($1,$2,$3,$4::jsonb,$5)
        ON CONFLICT (shop_id, rule_key) DO UPDATE
        SET enabled = EXCLUDED.enabled,
            params = EXCLUDED.params,
            weekly_cap = EXCLUDED.weekly_cap,
            updated_at = now()
        RETURNING *
        """,
        shop_id, rule_key, enabled, json.dumps(params), weekly_cap,
    )
    return _parse_rule(row) if row else row


async def sent_this_week(shop_id: UUID, rule_key: str) -> int:
    """How many of this rule's sends went out this calendar week.

    The counter the owner sees against the weekly cap. A calendar week is the
    honest meaning of "questa settimana"; the cap is a commercial ceiling, not
    a Meta platform fact, so it does not need Meta's rolling window.
    """
    row = await execute_one(
        """
        SELECT count(*) AS n FROM whatsapp.automation_sends
        WHERE shop_id = $1 AND rule_key = $2
          AND sent_at >= date_trunc('week', now())
        """,
        shop_id, rule_key,
    )
    return int(row["n"]) if row else 0


async def record_automation_send(
    *, shop_id: UUID, rule_key: str, appointment_id: UUID,
) -> None:
    """Persist that one appointment was handled by one rule.

    Written AFTER the enqueue in the tick: a crash between enqueue and record
    re-sends once (recoverable), where the reverse order would silently drop a
    message forever.
    """
    await execute_void(
        """
        INSERT INTO whatsapp.automation_sends
            (shop_id, rule_key, appointment_id)
        VALUES ($1,$2,$3)
        ON CONFLICT (rule_key, appointment_id) DO NOTHING
        """,
        shop_id, rule_key, appointment_id,
    )


# ----------------------------------------------------------------- due work

# Shared projection: the four facts the UTILITY templates need plus the send
# address. `split_part` on the first space keeps "Maria Luisa" -> "Maria",
# which is the register the template was approved in.
_DUE_PROJECTION = """
    a.id            AS appointment_id,
    a.customer_id   AS customer_id,
    c.phone         AS phone,
    split_part(c.full_name, ' ', 1) AS first_name,
    s.name          AS shop_name,
    COALESCE((
        SELECT string_agg(svc.service_name, ', ' ORDER BY svc.service_name)
        FROM business_app_core.appointment_services asv
        JOIN business_app_core.services svc ON svc.id = asv.service_id
        WHERE asv.appointment_id = a.id
    ), '')          AS service_names
"""

# The customer must be reachable — a phone is the whole point of the exercise.
_CUSTOMER_REACHABLE = """
    AND c.phone IS NOT NULL AND c.phone <> ''
"""


async def due_feedback(shop_id: UUID, days_after: int) -> list[dict]:
    """Completed appointments whose end was `days_after` days ago.

    A one-day window, not "older than": the appointment is due the day it
    becomes `days_after` days old and stays due for 24 hours. Re-runs are safe
    because `automation_sends` is keyed on the appointment id.
    """
    return await execute(
        f"""
        SELECT {_DUE_PROJECTION}, a.end_time AS appointment_at
        FROM business_app_core.appointments a
        JOIN business_app_core.customers c ON c.id = a.customer_id
        JOIN business_app_core.shops s ON s.id = a.shop_id
        WHERE a.shop_id = $1
          AND a.status = 'completed'
          AND a.end_time > now() - make_interval(days => $2 + 1)
          AND a.end_time <= now() - make_interval(days => $2)
          {_CUSTOMER_REACHABLE}
          AND NOT EXISTS (
              SELECT 1 FROM whatsapp.automation_sends as_send
              WHERE as_send.rule_key = 'feedback'
                AND as_send.appointment_id = a.id
          )
        """,
        shop_id, days_after,
    )


async def due_reminders(shop_id: UUID, hours_before: int) -> list[dict]:
    """Booked appointments starting within the next `hours_before` hours.

    "Booked" means not yet started and not cancelled: status is still
    scheduled/confirmed. Same one-shot guarantee as due_feedback — the send log
    makes re-running the tick idempotent.
    """
    return await execute(
        f"""
        SELECT {_DUE_PROJECTION}, a.start_time AS appointment_at
        FROM business_app_core.appointments a
        JOIN business_app_core.customers c ON c.id = a.customer_id
        JOIN business_app_core.shops s ON s.id = a.shop_id
        WHERE a.shop_id = $1
          AND a.status IN ('scheduled','confirmed')
          AND a.start_time > now()
          AND a.start_time <= now() + make_interval(hours => $2)
          {_CUSTOMER_REACHABLE}
          AND NOT EXISTS (
              SELECT 1 FROM whatsapp.automation_sends as_send
              WHERE as_send.rule_key = 'reminder'
                AND as_send.appointment_id = a.id
          )
        """,
        shop_id, hours_before,
    )
