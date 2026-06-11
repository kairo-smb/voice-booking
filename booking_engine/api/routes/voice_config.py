"""Voice agent Layer 1 config GET/PATCH endpoints."""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from booking_engine.api.deps import require_control_plane_token
from booking_engine.db.voice_config_queries import get_config, upsert_config
from booking_engine.db.voice_telephony_queries import get_telephony
from booking_engine.db.voice_tone_queries import get_tone_by_id
from booking_engine.services.phone_normalize import digits_only

router = APIRouter(prefix="/voice/config", tags=["voice-config"])


_PATCHABLE_FIELDS = {
    "enabled", "display_name", "greeting_after_disclosure",
    "voice_preset", "tone_id", "business_hours",
    "answer_mode", "overflow_ring_count",
    "services_to_mention", "retention_days",
    "manual_fallback_number",
    "auto_topup_enabled", "auto_topup_threshold_tokens", "auto_topup_package_id",
}


class ConfigPatch(BaseModel):
    enabled: bool | None = None
    display_name: str | None = None
    greeting_after_disclosure: str | None = None
    voice_preset: str | None = Field(default=None, pattern=r"^(warm_female|neutral_female|neutral_male)$")
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
