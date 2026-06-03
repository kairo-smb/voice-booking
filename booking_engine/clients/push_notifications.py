"""Push notification client (stub).

Plan C wires this to the webapp's existing notification infrastructure.
For now, this logs the event so behavior is observable and tests can mock it.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


async def send_push(
    *, shop_id: UUID, event: str, payload: dict[str, Any] | None = None
) -> None:
    """Send a push event to all merchant devices subscribed for this shop."""
    logger.info("push %s shop_id=%s payload=%s", event, shop_id, payload or {})
    # Plan C: forward to webapp /api/v1/notifications/push endpoint.
