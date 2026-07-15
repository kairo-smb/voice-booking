"""Path 1 forwarding heartbeat — finds shops whose forwarded number went silent.

Path 1 = 'forward' setup (default, ~70-80% of shops): the salon's existing
carrier number forwards to our Telnyx DID. This heartbeat detects when that
forwarding has silently stopped (no inbound call in threshold_days).

Run nightly by a Lambda scheduled event. For each silent shop, emit a push
event 'forwarding_might_be_off'. Webapp surfaces this as a persistent banner.
"""
from __future__ import annotations

from uuid import UUID

from booking_engine.clients.push_notifications import send_push
from booking_engine.db.connection import execute


async def find_silent_forwarded_shops(*, threshold_days: int = 5) -> list[dict]:
    """Return silent Path 1 (forward) shops that are in always_on mode.

    Overflow shops legitimately receive zero AI calls for days (staff answers
    everything), so silence there is normal — excluding them by joining
    shop_config.answer_mode avoids false 'forwarding might be off' alerts.
    """
    return await execute(
        """
        SELECT t.shop_id, t.kairo_number, t.last_inbound_call_at, t.setup_path
        FROM voice_agent.shop_telephony t
        JOIN voice_agent.shop_config c ON c.shop_id = t.shop_id
        WHERE t.setup_path = 'forward'
        AND c.answer_mode = 'always_on'
        AND (
            t.last_inbound_call_at IS NULL
            OR t.last_inbound_call_at < now() - ($1 || ' days')::interval
        )
        """,
        str(threshold_days),
    )


async def emit_heartbeat_alerts(*, threshold_days: int = 5) -> int:
    """Find silent forwarded shops and emit a push for each. Returns count emitted."""
    silent = await find_silent_forwarded_shops(threshold_days=threshold_days)
    for row in silent:
        await send_push(
            shop_id=UUID(str(row["shop_id"])),
            event="forwarding_might_be_off",
            payload={
                "kairo_number": row.get("kairo_number"),
                "last_inbound_call_at": str(row.get("last_inbound_call_at")) if row.get("last_inbound_call_at") else None,
            },
        )
    return len(silent)
