"""Voice agent Layer 1 config GET/PATCH endpoints."""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from booking_engine.api.deps import require_control_plane_token
from booking_engine.db import service_intake_queries as intake
from booking_engine.db.voice_config_queries import get_config, upsert_config
from booking_engine.db.voice_telephony_queries import get_telephony
from booking_engine.db.voice_tone_queries import get_tone_by_id, list_preset_tones
from booking_engine.services.phone_normalize import digits_only

router = APIRouter(prefix="/voice/config", tags=["voice-config"])


_PATCHABLE_FIELDS = {
    "enabled", "display_name", "greeting_after_disclosure", "greeting_overflow",
    "voice_preset", "tone_id", "business_hours",
    "answer_mode", "overflow_ring_count",
    "services_to_mention", "retention_days",
    "manual_fallback_number",
    "auto_topup_enabled", "auto_topup_threshold_tokens", "auto_topup_package_id",
    # The WhatsApp booking agent's opt-in (migration 25). Patched here and not
    # under /whatsapp for the same reason the intake questions are: it is a
    # property of the shop's agent, and shop_config is where those live. The
    # column is NOT NULL DEFAULT false, so the refusal survives a shop that
    # never touches this endpoint — the UI is not what keeps it off.
    "whatsapp_agent_enabled",
}


class ConfigPatch(BaseModel):
    enabled: bool | None = None
    display_name: str | None = None
    greeting_after_disclosure: str | None = None
    greeting_overflow: str | None = None
    voice_preset: str | None = Field(default=None, pattern=r"^(alloy|ash|ballad|coral|echo|sage|shimmer|verse)$")
    tone_id: UUID | None = None
    business_hours: dict | None = None
    answer_mode: str | None = Field(default=None, pattern=r"^(overflow|always_on)$")
    overflow_ring_count: int | None = Field(default=None, ge=1, le=10)
    services_to_mention: list[UUID] | None = None
    retention_days: int | None = Field(default=None, ge=30, le=365)
    manual_fallback_number: str | None = None
    auto_topup_enabled: bool | None = None
    auto_topup_threshold_tokens: int | None = Field(default=None, ge=0)
    auto_topup_package_id: UUID | None = None
    whatsapp_agent_enabled: bool | None = None


class IntakePut(BaseModel):
    """Deliberately without `max_length`: over the cap is truncated, not refused.

    The webapp counts the characters down in front of the owner, so 2001 is a
    UI state, not a request anyone should ever be able to send. If one arrives
    anyway — a second client, a retry of an older draft — storing the first 2000
    characters is a better answer than a 422 the owner cannot interpret.
    """
    questions: str = ""


@router.get("/tones")
async def list_tones(
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict[str, Any]:
    return {"data": await list_preset_tones()}


# Intake questions live here, under /voice/config, and not under /whatsapp:
# what to ask before booking a colour is the same knowledge whichever channel
# is asking, and the phone agent is the next thing to read it.

@router.get("/{shop_id}/intake")
async def get_intake(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict[str, Any]:
    """Rows only — service names come from `business_app_core`, which the
    caller already has and this repo does not own."""
    return {"data": await intake.for_shop(shop_id)}


@router.put("/{shop_id}/intake/{service_id}", response_model=None)
async def put_intake(
    shop_id: UUID,
    service_id: UUID,
    body: IntakePut,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
):
    """PUT, not PATCH: the whole value is what was typed, and clearing the
    field is a real edit rather than an omission."""
    row = await intake.set_questions(
        shop_id=shop_id, service_id=service_id, questions=body.questions,
    )
    if row is None:
        # Not owned by this shop, or not a service at all. One answer for both:
        # a caller for another shop learns nothing either way.
        return JSONResponse(
            status_code=404,
            content={"error": "Unknown service for this shop."},
        )
    return {"data": row}


@router.get("/{shop_id}")
async def get_for_shop(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict[str, Any]:
    row = await get_config(shop_id)
    return {"data": row}


@router.patch("/{shop_id}", response_model=None)
async def patch_for_shop(
    shop_id: UUID,
    body: ConfigPatch,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
):
    payload = body.model_dump(exclude_unset=True, exclude_none=False)
    payload = {k: v for k, v in payload.items() if k in _PATCHABLE_FIELDS}

    # Tone existence validation
    if "tone_id" in payload and payload["tone_id"] is not None:
        tone = await get_tone_by_id(payload["tone_id"])
        if tone is None:
            return JSONResponse(
                status_code=400,
                content={"error": "Unknown tone_id; no matching voice_tones row."},
            )

    # Loop-safety validation: fallback must differ from forwarded number
    if payload.get("manual_fallback_number"):
        normalized_new = digits_only(payload["manual_fallback_number"])
        telephony = await get_telephony(shop_id)
        if telephony and telephony.get("salon_existing_normalized"):
            if normalized_new == telephony["salon_existing_normalized"]:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Fallback number creates a forwarding loop "
                             "with the salon's existing number."},
                )

    if not payload:
        existing = await get_config(shop_id)
        return {"data": existing}

    row = await upsert_config(shop_id, **payload)
    return {"data": row}
