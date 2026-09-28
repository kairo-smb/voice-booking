"""Session-owned writes for the customer agents (marketing-engine).

The customer agents — WhatsApp now, voice after Phase C — run in the
marketing-engine, and every booking/customer write goes to the webapp. What
stays here is what this repo owns: the `voice_agent.calls` session row. Three
writes touch it — linking the identified customer, escalating to the owner,
recording the outcome — and they are these routes.

**Scoped by `X-Shop-Id` against the row, never by the id alone.** The tool
token is shared by every salon, and a `call_id` travels through a model's
context; a session that is not the header shop's is answered exactly like a
session that does not exist (404 `unknown_session`), so the difference between
"not yours" and "not there" is never something a caller can probe.

Root-mounted, like `/voice/tools/*`: the marketing-engine reaches this service
through `VOICE_AGENT_TOOLS_URL`, which by contract carries no `/api/v1`
(AGENTS.md 2026-09-22).

The escalation and outcome bodies are the `/voice/tools/escalate_to_merchant`
and `/mark_outcome` handlers, moved unchanged. Those two routes stay until
Phase C moves the voice agent onto the marketing-engine too, and are deleted
with the rest of the voice tool layer then.
"""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import EscalateIn, Envelope
from booking_engine.clients.push_notifications import send_push
from booking_engine.db.voice_calls_queries import (
    get_call, insert_callback_memo, set_call_outcome,
)
from booking_engine.db.voice_queries import link_customer
from booking_engine.db.voice_tool_queries import get_customer_shop_id

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCustomerIn(BaseModel):
    customer_id: UUID


class SessionOutcomeIn(BaseModel):
    # The `calls.outcome` CHECK values, plus `info_only`: the agents' schema
    # names the informational outcome that way (the naming plan, A2.3), and the
    # column spells it `info`. Accepting both here is cheaper than a CHECK
    # migration over a column the voice path and the analytics already read.
    outcome: Literal[
        "booked", "rescheduled", "cancelled", "info", "info_only",
        "abandoned", "escalated", "failed",
    ]
    summary: str | None = None


def _refuse(error: str) -> JSONResponse:
    """404 with the envelope body. The body's `error` is what tells the
    marketing-engine this is a refusal, not a missing route (a transport
    failure on its side)."""
    return JSONResponse(status_code=404,
                        content={"ok": False, "data": None, "error": error})


async def _session(call_id: UUID, shop_id: UUID) -> dict | None:
    call = await get_call(call_id)
    if not call or call.get("shop_id") != shop_id:
        return None
    return call


@router.post("/{call_id}/customer")
async def link_session_customer(
    call_id: UUID,
    body: SessionCustomerIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
):
    if not await _session(call_id, x_shop_id):
        return _refuse("unknown_session")
    # "No such customer" and "another salon's customer" are one refusal for
    # the same reason as the session check above.
    if await get_customer_shop_id(customer_id=body.customer_id) != x_shop_id:
        return _refuse("unknown_customer")
    await link_customer(x_shop_id, call_id, body.customer_id)
    return Envelope[dict](ok=True, data={"linked": True})


@router.post("/{call_id}/escalation")
async def escalate_session(
    call_id: UUID,
    body: EscalateIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
):
    call = await _session(call_id, x_shop_id)
    if not call:
        return _refuse("unknown_session")
    caller_phone = call.get("caller_number")
    memo_id = await insert_callback_memo(
        call_id=call_id, shop_id=call["shop_id"],
        customer_id=call.get("customer_id"),
        caller_phone=caller_phone,
        reason=f"{body.reason} — {body.customer_message}",
        callback_window=body.callback_window,
    )
    # outcome='escalated' IS the session's escalated flag: `wa_session_queries`
    # derives the thread's escalated state from it, so this also silences the
    # WhatsApp agent on the thread.
    await set_call_outcome(
        call_id=call_id, outcome="escalated",
        summary=body.customer_message,
        callback_window=body.callback_window,
    )
    await send_push(
        shop_id=call["shop_id"], event="voice_new_memo",
        payload={"memo_id": str(memo_id), "reason": body.reason,
                 "caller_phone": caller_phone},
    )
    return Envelope[dict](ok=True, data={"memo_id": str(memo_id)})


@router.post("/{call_id}/outcome")
async def set_session_outcome(
    call_id: UUID,
    body: SessionOutcomeIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
):
    if not await _session(call_id, x_shop_id):
        return _refuse("unknown_session")
    await set_call_outcome(
        call_id=call_id,
        outcome="info" if body.outcome == "info_only" else body.outcome,
        summary=body.summary, callback_window=None,
    )
    return Envelope[dict](ok=True, data={"marked": True})
