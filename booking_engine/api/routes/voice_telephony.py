"""Telephony provisioning + listing endpoints (Path 1 + Path 2 onboarding)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from twilio.base.exceptions import TwilioRestException

from booking_engine.api.deps import require_control_plane_token
from booking_engine.clients.twilio_numbers import (
    purchase_number,
    release_number,
    search_available_numbers,
)
from booking_engine.config import Settings, get_settings
from booking_engine.db import number_request_queries as rq
from booking_engine.db import voice_telephony_queries as q
from booking_engine.db.voice_config_queries import get_config
from booking_engine.services.number_provisioning import submit_request
from booking_engine.services.setup_instructions import build_instructions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice/numbers", tags=["voice-telephony"])


class AvailableNumberOut(BaseModel):
    phone_number: str
    friendly_name: str
    locality: str
    region: str


class ProvisionIn(BaseModel):
    shop_id: UUID
    phone_number: str
    setup_path: str = Field(pattern=r"^(new|forward)$")
    salon_existing_number: str | None = None


class TelephonyOut(BaseModel):
    shop_id: UUID
    kairo_number: str
    kairo_number_sid: str
    setup_path: str
    salon_existing_number: str | None


class NumberRequestOut(BaseModel):
    shop_id: UUID
    status: str
    regulation_sid: str | None = None
    bundle_sid: str | None = None
    end_user_sid: str | None = None
    document_sid: str | None = None
    business_name: str | None = None
    contact_email: str | None = None
    evaluation_errors: object | None = None
    rejection_reason: str | None = None
    created_at: datetime | None = None
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    updated_at: datetime | None = None


def _request_out(row: dict) -> dict:
    """Serialise a raw voice_agent.number_requests row for the API.

    evaluation_errors is jsonb, which asyncpg (no codec configured on this
    pool — see booking_engine/db/connection.py) returns as a raw JSON string,
    not a parsed object. Decode it here so the webapp gets real JSON, not a
    JSON-encoded string inside JSON.
    """
    data = dict(row)
    raw_errors = data.get("evaluation_errors")
    if isinstance(raw_errors, str):
        data["evaluation_errors"] = json.loads(raw_errors)
    return NumberRequestOut(**data).model_dump(mode="json")


def _telephony_out(row: dict) -> dict:
    return TelephonyOut(
        shop_id=row["shop_id"],
        kairo_number=row["kairo_number"],
        kairo_number_sid=row["kairo_number_sid"],
        setup_path=row["setup_path"],
        salon_existing_number=row["salon_existing_number"],
    ).model_dump(mode="json")


@router.get("/search")
async def search(
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    area_code: str | None = Query(default=None, max_length=4),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    results = search_available_numbers(
        area_code=area_code,
        country=settings.twilio_default_country,
        limit=limit,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )
    return {"data": [AvailableNumberOut(**r.__dict__).model_dump() for r in results]}


@router.post("/provision")
async def provision(
    body: ProvisionIn,
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    if body.setup_path == "forward" and not body.salon_existing_number:
        raise HTTPException(400, "salon_existing_number required for forward setup")

    # Idempotent: a shop that already has telephony is returned as-is, with
    # no Twilio call at all — a second /provision call must not buy a second
    # number and orphan the first (see insert_telephony's docstring).
    existing = await q.get_telephony(body.shop_id)
    if existing is not None:
        return {"data": _telephony_out(existing)}

    voice_url = f"{settings.public_base_url}/api/v1/voice/twiml/incoming"
    try:
        purchased = purchase_number(
            phone_number=body.phone_number,
            voice_url=voice_url,
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            bundle_sid=settings.twilio_bundle_sid or None,
            address_sid=settings.twilio_address_sid or None,
        )
    except TwilioRestException as exc:
        raise HTTPException(502, f"Twilio number purchase failed: {exc.msg}") from exc
    # The one Kairo-entity regulatory Bundle is approved once, out-of-band,
    # before any shop onboards (see the migration spec) — every purchase
    # after that is synchronous: it succeeds now (active) or raises outright.
    row = await q.insert_telephony(
        shop_id=body.shop_id,
        provider="twilio",
        kairo_number=purchased.phone_number,
        kairo_number_sid=purchased.sid,
        salon_existing_number=body.salon_existing_number,
        setup_path=body.setup_path,
    )
    if row is None:
        # Lost a race to a concurrent /provision call for the same shop — the
        # number we just bought has nothing referencing it. Release it back
        # to Twilio rather than leak it; a failed release must not turn into
        # a 500 that hides the leak, so it's logged, not raised.
        try:
            release_number(
                sid=purchased.sid,
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
            )
        except Exception:  # noqa: BLE001 — see number_provisioning.provision_approved
            logger.exception(
                "voice_numbers.provision.release_failed shop=%s sid=%s — number is "
                "LEAKED and must be released manually in the Twilio console",
                body.shop_id, purchased.sid,
            )
        winner = await q.get_telephony(body.shop_id)
        return {"data": _telephony_out(winner)}
    return {"data": _telephony_out(row)}


@router.get("/{shop_id}/setup-instructions")
async def setup_instructions(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    telephony = await q.get_telephony(shop_id)
    if not telephony:
        raise HTTPException(404, "No telephony provisioned for this shop")
    config = await get_config(shop_id) or {}
    return {"data": build_instructions(
        kairo_number=telephony["kairo_number"],
        salon_existing_number=telephony.get("salon_existing_number"),
        answer_mode=config.get("answer_mode", "overflow"),
        overflow_ring_count=config.get("overflow_ring_count", 4),
    )}


@router.get("/{shop_id}")
async def get_for_shop(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    row = await q.get_telephony(shop_id)
    if not row:
        return {"data": None}
    return {"data": _telephony_out(row)}


@router.post("/request")
async def request_number(
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    shop_id: Annotated[UUID, Form(...)],
    business_name: Annotated[str, Form(..., min_length=1)],
    contact_email: Annotated[str, Form(..., min_length=1)],
    document: UploadFile,
) -> dict:
    content = await document.read()
    result = await submit_request(
        shop_id=shop_id,
        business_name=business_name,
        contact_email=contact_email,
        filename=document.filename,
        content=content,
        content_type=document.content_type or "application/octet-stream",
        settings=settings,
    )
    return {"data": result}


@router.get("/request/{shop_id}")
async def get_request_status(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """The webapp's poll target: what request state (if any) + what
    telephony (if any) exist for a shop, so it can decide which UI state
    to render (no request yet / draft / pending review / provisioned)."""
    request_row = await rq.get_request(shop_id)
    telephony_row = await q.get_telephony(shop_id)
    return {"data": {
        "request": _request_out(request_row) if request_row else None,
        "telephony": _telephony_out(telephony_row) if telephony_row else None,
    }}
