"""SQL for the WABA action audit (`whatsapp.audit_events`, migration 20).

Fail-open on purpose: recording who did what must never break the action it
logs. `record_audit_event` swallows its own failures (log + return None), so
every route handler can call it without ceremony and without a try/except.
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

from booking_engine.db.connection import execute_void

logger = logging.getLogger(__name__)


async def record_audit_event(
    *,
    shop_id: UUID,
    event: str,
    actor_id: UUID | None = None,
    source: str | None = None,
    campaign_key: str | None = None,
    template_name: str | None = None,
    is_template: bool | None = None,
    recipient_count: int | None = None,
    status: str | None = None,
    http_status: int | None = None,
    error_message: str | None = None,
    request: dict | None = None,
    response: dict | None = None,
) -> None:
    """Insert one audit row. Never raises — a dead audit DB must not break a send."""
    try:
        await execute_void(
            """
            INSERT INTO whatsapp.audit_events
                (shop_id, actor_id, source, event, campaign_key, template_name,
                 is_template, recipient_count, status, http_status,
                 error_message, request, response)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb)
            """,
            shop_id, actor_id, source, event, campaign_key, template_name,
            is_template, recipient_count, status, http_status, error_message,
            json.dumps(request) if request is not None else None,
            json.dumps(response) if response is not None else None,
        )
    except Exception:  # noqa: BLE001 — the audit must never take the action down
        logger.exception("whatsapp.audit_failed event=%s shop=%s", event, shop_id)
