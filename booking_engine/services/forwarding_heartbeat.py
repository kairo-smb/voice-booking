"""Path-2 forwarding heartbeat — finds shops whose forwarded number went silent.

Run nightly by a Lambda scheduled event. For each silent shop, emit a push
event 'forwarding_might_be_off'. Webapp surfaces this as a persistent banner.
"""
from __future__ import annotations

from booking_engine.db.connection import execute


async def find_silent_forwarded_shops(*, threshold_days: int = 5) -> list[dict]:
    """Return Path-2 shops with no inbound call in the last `threshold_days`."""
    return await execute(
        """
        SELECT shop_id, kairo_number, last_inbound_call_at, setup_path
        FROM voice_agent.shop_telephony
        WHERE setup_path = 'forward'
        AND (
            last_inbound_call_at IS NULL
            OR last_inbound_call_at < now() - ($1 || ' days')::interval
        )
        """,
        str(threshold_days),
    )
