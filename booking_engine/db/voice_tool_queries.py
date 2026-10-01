"""Customer and catalogue reads still served from here.

Until 2026-09-28 this held the voice agent tools' SQL, customers, services and
appointment writes. The tools moved to marketing-engine's customer agents
(AGENTS.md, 2026-09-28) and their writes to the webapp's `/agent/*` routes;
what is left are the three reads other code in this repo still uses:
`identity_resolver` (caller lookup), `api/routes/sessions.py` (customer
ownership) and the service lists behind `voice.py` and `voice_memos.py`.
"""
from __future__ import annotations

from uuid import UUID

from booking_engine.db import connection


async def find_customers_by_phone(*, shop_id: UUID, phone_digits: str) -> list[dict]:
    """Find customers whose phone normalizes to the same digits."""
    if not phone_digits:
        return []
    # Ground-truth customers has full_name/tags (no first/last, no last_visit).
    # Alias to the keys identity_resolver/CustomerSummary expect.
    return await connection.execute(
        """
        SELECT id, full_name AS first_name, NULL::text AS last_name,
               NULL::timestamptz AS last_visit_at,
               preferred_staff_id, tags AS notes_tags, verified
        FROM business_app_core.customers
        WHERE shop_id = $1 AND phone_normalized = $2
        LIMIT 5
        """,
        shop_id, phone_digits,
    )


async def get_customer_shop_id(*, customer_id: UUID) -> UUID | None:
    """Which shop owns this customer. None when there is no such customer.

    The ownership side of an authorization check, kept separate from the
    write so "no such customer" and "not yours" stay distinguishable —
    folding `AND shop_id = $2` into the UPDATE would collapse both into an
    unexplained zero-row result.
    """
    row = await connection.execute_one(
        "SELECT shop_id FROM business_app_core.customers WHERE id = $1",
        customer_id,
    )
    return row["shop_id"] if row else None


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

