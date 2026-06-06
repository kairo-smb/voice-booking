"""Lifecycle tools — mark_outcome and escalate_to_merchant."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import (
    EscalateIn, Envelope, MarkOutcomeIn,
)
from booking_engine.clients.push_notifications import send_push
from booking_engine.db.voice_calls_queries import (
    get_call, insert_callback_memo, set_call_outcome,
)

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-lifecycle"])


@router.post("/mark_outcome")
async def mark_outcome(
    body: MarkOutcomeIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    await set_call_outcome(
        call_id=x_call_id, outcome=body.outcome, summary=body.summary,
        callback_window=body.callback_window,
    )
    return Envelope[dict](ok=True, data={"marked": True})


@router.post("/escalate_to_merchant")
async def escalate_to_merchant(
    body: EscalateIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    call = await get_call(x_call_id)
    if not call:
        return Envelope[dict](ok=False, error="call_not_found")
    memo_id = await insert_callback_memo(
        call_id=x_call_id, shop_id=call["shop_id"],
        customer_id=call.get("matched_customer_id"),
        caller_phone=call.get("caller_phone"),
        reason=f"{body.reason} — {body.customer_message}",
        callback_window=body.callback_window,
    )
    await set_call_outcome(
        call_id=x_call_id, outcome="escalated",
        summary=body.customer_message,
        callback_window=body.callback_window,
    )
    await send_push(
        shop_id=call["shop_id"], event="voice_new_memo",
        payload={"memo_id": str(memo_id), "reason": body.reason,
                 "caller_phone": call.get("caller_phone")},
    )
    return Envelope[dict](ok=True, data={"memo_id": str(memo_id)})