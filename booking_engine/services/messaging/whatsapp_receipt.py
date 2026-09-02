"""Smart Receipt: send one PDF receipt as a WhatsApp document.

Unlike the marketing campaign path (queue + drip, see whatsapp_send.py), a
receipt is a single, synchronous, immediate send: the customer just paid, the
webapp renders the PDF and posts it here to upload and deliver as the DOCUMENT
header of Meta's `purchase_receipt_1` utility template.

The template is NOT in the marketing CATALOGUE — it is a document header, not a
body-with-variables — so it has its own ensure step (`ensure_receipt_template`),
gated on Meta having approved the same-named template on Kairo's own WABA first,
exactly like the catalogue gate.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from uuid import UUID

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging.whatsapp_onboarding import TEMPLATE_STATUS
from booking_engine.services.messaging.whatsapp_pricing import estimate_usd
from booking_engine.services.messaging.whatsapp_templates import (
    RECEIPT_TEMPLATE_BODY,
    RECEIPT_TEMPLATE_KEY,
    RECEIPT_TEMPLATE_LANGUAGE,
    RECEIPT_TEMPLATE_NAME,
)
from booking_engine.services.phone_normalize import normalize_e164

logger = logging.getLogger(__name__)


async def ensure_receipt_template(*, shop_id: UUID, settings) -> dict:
    """Create `purchase_receipt_1` on the salon's WABA if missing.

    Gated on Kairo's own copy being `approved` first, for the same reason as the
    catalogue gate: a template rejection is Meta's judgment on the content,
    identical whatever WABA it is submitted to, so it is vetted once on Kairo's
    WABA before being pushed to any customer's. Fails closed when the gate is
    unconfigured or the sample document URL is missing.
    """
    sender = await wq.get_sender(shop_id)
    if not sender or not sender.get("waba_id") or not sender.get("access_token"):
        return {"ok": False, "error": "not_started"}
    if await wq.get_template(shop_id, RECEIPT_TEMPLATE_KEY):
        return {"ok": True, "created": 0}

    if not settings.meta_kairo_waba_id or not settings.meta_kairo_token:
        return {"ok": False, "error": "kairo_waba_not_configured"}
    if not settings.meta_receipt_sample_url:
        return {"ok": False, "error": "receipt_sample_not_configured"}

    verdict = await meta.fetch_template(
        waba_id=settings.meta_kairo_waba_id,
        name=RECEIPT_TEMPLATE_NAME,
        token=settings.meta_kairo_token,
    )
    if not verdict or verdict.status != "approved":
        return {"ok": False, "error": "not_ready"}

    try:
        meta_id, status = await meta.create_document_template(
            waba_id=sender["waba_id"], token=sender["access_token"],
            name=RECEIPT_TEMPLATE_NAME, language=RECEIPT_TEMPLATE_LANGUAGE,
            category="UTILITY", body_text=RECEIPT_TEMPLATE_BODY,
            example_url=settings.meta_receipt_sample_url,
        )
    except meta.MetaError as exc:
        logger.warning("whatsapp.receipt_template_create_failed shop=%s err=%s",
                       shop_id, exc)
        return {"ok": False, "error": "meta_error", "detail": str(exc)}

    await wq.upsert_template(
        shop_id=shop_id, template_key=RECEIPT_TEMPLATE_KEY,
        name=RECEIPT_TEMPLATE_NAME, meta_template_id=meta_id,
        language=RECEIPT_TEMPLATE_LANGUAGE, category="UTILITY",
        status=TEMPLATE_STATUS.get(status, "pending"), variable_count=0,
    )
    return {"ok": True, "created": 1}


async def send_receipt(
    *, shop_id: UUID, customer_id: UUID, phone: str, reference: str,
    filename: str, pdf_base64: str, initiated_by: UUID | None, settings,
) -> dict:
    """Upload the receipt PDF and send it as the document header of the template.

    Synchronous and fire-once: the webapp calls this after the payment is closed
    and does not queue it. A failure here is a receipt that didn't go out, not a
    lost campaign — the webapp's fire-and-forget caller logs it and moves on.
    """
    sender = await wq.get_sender(shop_id)
    if not sender or sender.get("status") != "online":
        return {"ok": False, "error": "sender_not_online"}
    if not sender.get("phone_number_id") or not sender.get("access_token"):
        return {"ok": False, "error": "sender_missing_credentials"}

    normalized = normalize_e164(phone or "")
    if not normalized:
        return {"ok": False, "error": "no_phone"}

    # Lazy self-heal: if the template isn't on this WABA yet, try once to create
    # it. Otherwise the first send after onboarding would always 409.
    if not await wq.get_template(shop_id, RECEIPT_TEMPLATE_KEY):
        ensured = await ensure_receipt_template(shop_id=shop_id, settings=settings)
        if not ensured.get("ok"):
            return ensured

    template = await wq.get_template(shop_id, RECEIPT_TEMPLATE_KEY)
    if not template:
        return {"ok": False, "error": "unknown_template"}
    if template["status"] != "approved":
        return {"ok": False, "error": f"template_{template['status']}"}

    try:
        pdf = base64.b64decode(pdf_base64, validate=False)
    except Exception:  # noqa: BLE001 — a bad payload is a refusal, not a crash
        return {"ok": False, "error": "invalid_pdf"}

    try:
        media_id = await meta.upload_media(
            phone_number_id=sender["phone_number_id"],
            token=sender["access_token"], filename=filename, content=pdf,
        )
        wamid = await meta.send_document_template(
            phone_number_id=sender["phone_number_id"],
            token=sender["access_token"], to=normalized,
            name=RECEIPT_TEMPLATE_NAME, language=RECEIPT_TEMPLATE_LANGUAGE,
            media_id=media_id, filename=filename,
        )
    except meta.MetaError as exc:
        return {"ok": False, "error": "meta_error", "detail": str(exc)}

    # Record for audit/status. campaign_key is None (a receipt is not a campaign),
    # so the campaign idempotency index does not apply and re-sends are legal.
    message_id = await wq.enqueue(
        shop_id=shop_id, customer_id=customer_id, campaign_key=None,
        to_phone=normalized, from_number=sender.get("phone_number") or "",
        template_name=RECEIPT_TEMPLATE_NAME, template_language=RECEIPT_TEMPLATE_LANGUAGE,
        variables={}, preview=RECEIPT_TEMPLATE_BODY,
        scheduled_at=datetime.now(timezone.utc), status="sent",
        initiated_by=initiated_by,
    )
    if message_id:
        await wq.mark_sent(
            message_id=message_id, provider_sid=wamid,
            price_usd=estimate_usd("utility"), credits=None,
        )
    return {"ok": True, "wamid": wamid}
