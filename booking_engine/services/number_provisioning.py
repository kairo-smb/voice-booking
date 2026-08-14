"""Bundle request + approved-purchase service for self-service number provisioning.

Two entry points:

- `submit_request` — the webapp's "Richiedi numero" call. Builds one salon's
  own Twilio regulatory bundle (Twilio's ISV rules forbid reusing Kairo's own
  business info across customer bundles): regulation -> End-User -> document
  -> Bundle -> 2x ItemAssignment -> Evaluate -> submit-if-compliant. Each
  Twilio SID is persisted the moment it exists (via `set_sids`), not batched
  at the end, so a crash halfway through leaves a resumable row rather than
  an orphaned Twilio object we hold no local reference to.

- `provision_approved` — called by the hourly tick once a bundle comes back
  approved. Purchase is the only irreversible step in this whole flow, so it
  is bracketed per docs/number-provisioning-design.md §3.1: check for an
  existing row first (idempotent, handles a double-tick or a retried cron
  run), and release the number back to Twilio if the insert loses a race to
  a concurrent caller — without that release, a lost race leaks a number
  billed at ~$3/mo forever with nothing referencing it.

See docs/number-provisioning-design.md for the full design.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from booking_engine.clients import push_notifications
from booking_engine.clients import twilio_numbers
from booking_engine.clients import twilio_regulatory
from booking_engine.db import number_request_queries
from booking_engine.db import voice_telephony_queries

logger = logging.getLogger(__name__)

# Estonia mobile-business is the only regulation for this country/number-type
# combination (queried at request time, never hardcoded — see
# docs/number-provisioning-design.md §2.1). These three constants describe
# *what we ask Twilio for*, not the regulation's own requirements.
DOC_TYPE = "commercial_registrar_excerpt"
ISO_COUNTRY = "EE"
NUMBER_TYPE = "mobile"


def _violations_to_dicts(violations) -> list[dict]:
    """Plain dicts for a jsonb column and an HTTP response — not dataclasses."""
    return [
        {"friendly_name": v.friendly_name, "description": v.description}
        for v in violations
    ]


async def submit_request(
    *,
    shop_id: UUID,
    business_name: str,
    contact_email: str,
    filename: str,
    content: bytes,
    content_type: str,
    settings,
) -> dict:
    """Build and, if compliant, submit one salon's regulatory bundle.

    Order: regulation SID -> End-User -> document -> Bundle -> 2x
    ItemAssignment -> Evaluate -> submit only if compliant. See the module
    docstring for why each SID is persisted as soon as it's created.
    """
    existing = await number_request_queries.get_request(shop_id)
    if existing is not None and existing.get("status") == "provisioned":
        return {"ok": True, "status": "provisioned"}

    account_sid = settings.twilio_account_sid
    auth_token = settings.twilio_auth_token

    regulation_sid = await twilio_regulatory.get_regulation_sid(
        iso_country=ISO_COUNTRY,
        number_type=NUMBER_TYPE,
        account_sid=account_sid,
        auth_token=auth_token,
    )
    if regulation_sid is None:
        # Nothing exists on the Twilio side yet — creating an End-User/
        # document/Bundle against a regulation we can't confirm would just
        # be orphaned Twilio state, so stop here.
        return {"ok": False, "error": "no_regulation"}

    await number_request_queries.upsert_request(
        shop_id=shop_id,
        business_name=business_name,
        contact_email=contact_email,
        regulation_sid=regulation_sid,
        status="draft",
    )

    end_user_sid = await twilio_regulatory.create_end_user(
        business_name=business_name,
        account_sid=account_sid,
        auth_token=auth_token,
    )
    await number_request_queries.set_sids(shop_id=shop_id, end_user_sid=end_user_sid)

    document_sid = await twilio_regulatory.upload_document(
        business_name=business_name,
        doc_type=DOC_TYPE,
        filename=filename,
        content=content,
        content_type=content_type,
        account_sid=account_sid,
        auth_token=auth_token,
    )
    await number_request_queries.set_sids(shop_id=shop_id, document_sid=document_sid)

    bundle_sid = await twilio_regulatory.create_bundle(
        regulation_sid=regulation_sid,
        iso_country=ISO_COUNTRY,
        email=contact_email,
        friendly_name=business_name,
        account_sid=account_sid,
        auth_token=auth_token,
    )
    await number_request_queries.set_sids(shop_id=shop_id, bundle_sid=bundle_sid)

    await twilio_regulatory.assign_item(
        bundle_sid=bundle_sid,
        object_sid=end_user_sid,
        account_sid=account_sid,
        auth_token=auth_token,
    )
    await twilio_regulatory.assign_item(
        bundle_sid=bundle_sid,
        object_sid=document_sid,
        account_sid=account_sid,
        auth_token=auth_token,
    )

    compliant, violations = await twilio_regulatory.evaluate(
        bundle_sid=bundle_sid, account_sid=account_sid, auth_token=auth_token,
    )
    if not compliant:
        errors = _violations_to_dicts(violations)
        await number_request_queries.set_status(
            shop_id=shop_id, status="draft", evaluation_errors=errors,
        )
        return {"ok": True, "status": "draft", "evaluation_errors": errors}

    await twilio_regulatory.submit_for_review(
        bundle_sid=bundle_sid, account_sid=account_sid, auth_token=auth_token,
    )
    await number_request_queries.set_status(
        shop_id=shop_id, status="pending_review", submitted_at_now=True,
    )
    return {"ok": True, "status": "pending_review"}


async def provision_approved(shop_id: UUID, *, settings) -> str:
    """Buy the number for an approved bundle.

    Purchase is the only irreversible step, so it is bracketed: check for an
    existing row first (idempotent), and hand the number back if the insert
    loses a race. Without the release, a lost race leaks a number billed at
    ~$3/mo forever. See docs/number-provisioning-design.md §3.1.
    """
    account_sid = settings.twilio_account_sid
    auth_token = settings.twilio_auth_token

    existing_telephony = await voice_telephony_queries.get_telephony(shop_id)
    if existing_telephony is not None:
        # Idempotent replay (double-tick, retried cron run) — the number is
        # already bought and stored. Nothing to do with Twilio.
        await number_request_queries.set_status(shop_id=shop_id, status="provisioned")
        return "already_provisioned"

    req = await number_request_queries.get_request(shop_id)
    if req is None or not req.get("bundle_sid"):
        return "no_bundle"

    found = await asyncio.to_thread(
        twilio_numbers.search_available_numbers,
        area_code=None,
        country=settings.twilio_default_country,
        limit=1,
        account_sid=account_sid,
        auth_token=auth_token,
    )
    if not found:
        return "no_numbers_available"

    purchased = await asyncio.to_thread(
        twilio_numbers.purchase_number,
        phone_number=found[0].phone_number,
        voice_url=f"{settings.public_base_url}/api/v1/voice/twiml/incoming",
        account_sid=account_sid,
        auth_token=auth_token,
        bundle_sid=req["bundle_sid"],  # the SALON's own bundle, never a shared one
        address_sid=settings.twilio_address_sid or None,
    )

    row = await voice_telephony_queries.insert_telephony(
        shop_id=shop_id,
        provider="twilio",
        kairo_number=purchased.phone_number,
        kairo_number_sid=purchased.sid,
        salon_existing_number=None,
        setup_path="new",
    )
    if row is None:
        # Lost the race to a concurrent provision (double-tick, retried cron
        # run racing another). Give the number back or it bills ~$3/mo
        # forever with nothing referencing it.
        await asyncio.to_thread(
            twilio_numbers.release_number,
            sid=purchased.sid,
            account_sid=account_sid,
            auth_token=auth_token,
        )
        return "raced_released"

    await number_request_queries.set_status(shop_id=shop_id, status="provisioned")

    # push_notifications.send_push is currently a stub that only logs
    # (booking_engine/clients/push_notifications.py). Emitted here for
    # consistency with the existing call-lifecycle/balance-alert emitters so
    # it becomes real the day that stub is wired up — not because anyone is
    # actually notified today.
    await push_notifications.send_push(
        shop_id=shop_id,
        event="number_request_approved",
        payload={"kairo_number": purchased.phone_number},
    )
    return "provisioned"
