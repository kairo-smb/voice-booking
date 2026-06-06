"""Telnyx number lifecycle webhooks (number.status.active / number.status.failed)."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from booking_engine.clients.push_notifications import send_push
from booking_engine.db.voice_telephony_queries import update_telephony_activation

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice/telnyx", tags=["voice-telnyx-webhooks"])


@router.post("/number-status", status_code=200)
async def number_status(body: dict) -> dict:
    """Receive Telnyx number order status change webhooks."""
    try:
        event_type = body["data"]["event_type"]
        phone_number = body["data"]["payload"]["phone_number"]
    except (KeyError, TypeError):
        logger.warning("telnyx.number_status: malformed payload: %s", body)
        return JSONResponse(status_code=400, content={"error": "malformed payload"})

    if event_type == "number.status.active":
        row = await update_telephony_activation(
            kairo_number=phone_number,
            activation_status="active",
        )
        if not row:
            logger.info("telnyx.number_status: unknown number %s", phone_number)
            return {"status": "unknown_number"}
        await send_push(
            shop_id=row["shop_id"],
            event="voice_number_activated",
            payload={"phone_number": phone_number},
        )
        return {"status": "activated"}

    elif event_type == "number.status.failed":
        reason = (
            body["data"]["payload"].get("regulatory_rejection_reason")
            or body["data"]["payload"].get("failure_reason", "unknown")
        )
        row = await update_telephony_activation(
            kairo_number=phone_number,
            activation_status="rejected",
            regulatory_rejection_reason=reason,
        )
        if not row:
            logger.info("telnyx.number_status: unknown number %s", phone_number)
            return {"status": "unknown_number"}
        await send_push(
            shop_id=row["shop_id"],
            event="voice_number_rejected",
            payload={"phone_number": phone_number, "reason": reason},
        )
        return {"status": "rejected"}

    else:
        logger.debug("telnyx.number_status: ignoring event_type=%s", event_type)
        return {"status": "ignored"}
