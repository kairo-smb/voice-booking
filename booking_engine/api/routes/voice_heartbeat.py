"""POST /voice/heartbeat/forwarding — triggered by EventBridge nightly.

Scans Path-2 (forwarded-number) shops and emits a push event for each that
has had no inbound call in the last `threshold_days` (default 5).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from booking_engine.api.deps import require_control_plane_token
from booking_engine.services.forwarding_heartbeat import emit_heartbeat_alerts

router = APIRouter(prefix="/voice/heartbeat", tags=["voice-heartbeat"])


@router.post("/forwarding")
async def forwarding(
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    threshold_days: int = Query(default=5, ge=1, le=30),
) -> dict:
    emitted = await emit_heartbeat_alerts(threshold_days=threshold_days)
    return {"data": {"alerts_emitted": emitted, "threshold_days": threshold_days}}
