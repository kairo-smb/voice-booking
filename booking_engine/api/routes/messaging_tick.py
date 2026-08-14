"""POST /messaging/tick — the single scheduled entry point.

One hourly cron hits this. It polls regulatory bundles that are under review,
provisions numbers whose bundles were approved, then refreshes the health
semaphore for every shop that has a number.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from booking_engine.api.deps import require_control_plane_token
from booking_engine.clients.twilio_regulatory import get_bundle_status
from booking_engine.config import Settings, get_settings
from booking_engine.db.number_request_queries import list_pending_review, set_status
from booking_engine.services.number_health import check_all
from booking_engine.services.number_provisioning import provision_approved

logger = logging.getLogger(__name__)

router = APIRouter(tags=["messaging-tick"])


@router.post("/messaging/tick")
async def tick(
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    reviewed = 0
    provisioned = 0
    rejected = 0
    errors = 0

    for row in await list_pending_review():
        shop_id = row["shop_id"]
        bundle_sid = row["bundle_sid"]
        try:
            status = await get_bundle_status(
                bundle_sid=bundle_sid,
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
            )
            reviewed += 1
            if status == "twilio-approved":
                await set_status(shop_id=shop_id, status="approved", reviewed_at_now=True)
                await provision_approved(shop_id, settings=settings)
                provisioned += 1
            elif status == "twilio-rejected":
                await set_status(
                    shop_id=shop_id,
                    status="rejected",
                    rejection_reason=status,
                    reviewed_at_now=True,
                )
                rejected += 1
            # anything else (pending-review, in review, twilio-provisionally-approved,
            # etc.) — leave it alone, the next tick will check again.
        except Exception:  # noqa: BLE001 — one bad shop must not abort the sweep
            logger.exception("messaging_tick.shop_failed shop_id=%s", shop_id)
            errors += 1

    health = await check_all(settings=settings)

    return {"data": {
        "reviewed": reviewed,
        "provisioned": provisioned,
        "rejected": rejected,
        "errors": errors,
        "health": health,
    }}
