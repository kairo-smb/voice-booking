"""Memo endpoints used by webapp to populate Inbox panel."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from booking_engine.api.deps import require_control_plane_token
from booking_engine.db.voice_calls_queries import (
    count_pending_memos, list_memos, update_memo_status,
)

router = APIRouter(prefix="/voice/memos", tags=["voice-memos"])


class MemoPatch(BaseModel):
    status: str
    actioned_by: UUID | None = None


@router.get("/{shop_id}")
async def list_for_shop(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    status: str | None = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    rows = await list_memos(shop_id=shop_id, status=status, limit=limit)
    return {"data": rows}


@router.get("/{shop_id}/count")
async def action_center_count(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Action Center tile: number of open escalations for the shop."""
    n = await count_pending_memos(shop_id=shop_id)
    return {"data": {"pending_escalations": n}}


@router.patch("/{memo_id}")
async def action_memo(
    memo_id: UUID,
    body: MemoPatch,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    ok = await update_memo_status(
        memo_id=memo_id, status=body.status, actioned_by=body.actioned_by,
    )
    return {"data": {"updated": ok}}