"""POST /messaging/tick — the single scheduled entry point.

One hourly cron hits this. It polls regulatory bundles that are under review,
provisions numbers whose bundles were approved, refreshes the health
semaphore for every shop that has a number, runs the number-release sweep
(grace-period release of lapsed-plan shops' Twilio numbers), then polls
WhatsApp sender/template approvals and drips out the marketing due this hour.
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
from booking_engine.services.number_release import sweep as release_sweep
from booking_engine.services.messaging.whatsapp_automations import run_automations as whatsapp_run_automations
from booking_engine.services.messaging.whatsapp_onboarding import sweep as whatsapp_sweep
from booking_engine.services.messaging.whatsapp_send import send_due as whatsapp_send_due

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

    # Runs last, deliberately: a failure in the release sweep must never
    # prevent health from being refreshed for every shop above it. Wrapped
    # so a single Twilio hiccup here is counted, not raised — the cron
    # treats any non-2xx as a failed run, and one shop's release problem
    # should not mask the rest of this tick's work.
    try:
        release = await release_sweep(settings=settings)
    except Exception:  # noqa: BLE001 — see comment above
        logger.exception("messaging_tick.release_sweep_failed")
        release = {"scheduled": 0, "cleared": 0, "released": 0, "errors": 1}
        errors += 1

    # WhatsApp: poll Meta's verdicts (sender verification, template approval),
    # then drip out whatever marketing is due this hour. Same isolation as the
    # release sweep above — each stage is independently wrapped so one
    # failure can't suppress the others, and none of them can 500 the tick.
    try:
        whatsapp_onboarding_counts = await whatsapp_sweep(settings=settings)
    except Exception:  # noqa: BLE001 — see comment above
        logger.exception("messaging_tick.whatsapp_sweep_failed")
        whatsapp_onboarding_counts = {"errors": 1}
        errors += 1

    try:
        whatsapp_sends = await whatsapp_send_due(settings=settings)
    except Exception:  # noqa: BLE001 — see comment above
        logger.exception("messaging_tick.whatsapp_send_failed")
        whatsapp_sends = {"errors": 1}
        errors += 1

    # Automation rules run after the drip so a queued reminder is only ever
    # sent in a later tick (or the same one, if it lands before send_due
    # claims). Same isolation as every other stage: one failure is counted,
    # not raised, so it can never 500 the tick.
    try:
        whatsapp_automations = await whatsapp_run_automations(settings=settings)
    except Exception:  # noqa: BLE001 — see comment above
        logger.exception("messaging_tick.whatsapp_automations_failed")
        whatsapp_automations = {"errors": 1}
        errors += 1

    return {"data": {
        "reviewed": reviewed,
        "provisioned": provisioned,
        "rejected": rejected,
        "errors": errors,
        "health": health,
        "release": release,
        "whatsapp": whatsapp_onboarding_counts,
        "whatsapp_sends": whatsapp_sends,
        "whatsapp_automations": whatsapp_automations,
    }}
