"""OpenAI session-lifecycle webhooks.

session.started → identify caller, assemble system prompt, return as session update.
session.turn    → append transcript fragment to voice_agent.call_turns.
session.ended   → finalize call row, debit tokens, trigger post-hoc classifier if needed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import Envelope
from booking_engine.config import Settings, get_settings
from booking_engine.db.voice_calls_queries import (
    finalize_call, get_call, insert_call, insert_call_turn,
)
from booking_engine.db.voice_config_queries import get_config, get_policy
from booking_engine.services.identity_resolver import resolve_caller
from booking_engine.services.prompt_assembler import assemble_session_prompt
from booking_engine.services.token_meter import record_voice_debit

router = APIRouter(prefix="/voice/events", tags=["voice-events"])


class StartedIn(BaseModel):
    caller_phone: str | None = None
    openai_session_id: str | None = None


class TurnIn(BaseModel):
    role: str  # caller | agent | tool
    text: str
    seq: int


class EndedIn(BaseModel):
    duration_seconds: int
    tool_token_cost: int = 0
    ended_at: datetime


@router.post("/session.started")
async def session_started(
    body: StartedIn,
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[dict]:
    config = await get_config(x_shop_id)
    if not config:
        return Envelope[dict](ok=False, error="shop_config_missing")
    policy = await get_policy()
    if not policy:
        return Envelope[dict](ok=False, error="policy_missing")
    resolution = await resolve_caller(
        shop_id=x_shop_id, caller_phone=body.caller_phone,
    )
    matched_id = (
        resolution.unique_match.customer_id if resolution.unique_match else None
    )
    call_id = await insert_call(
        shop_id=x_shop_id, caller_phone=body.caller_phone,
        matched_customer_id=matched_id,
    )
    assembled = await assemble_session_prompt(
        config=config, policy=policy, resolution=resolution,
    )
    return Envelope[dict](ok=True, data={
        "call_id": str(call_id),
        "prompt": assembled.prompt,
        "tools": assembled.tools,
        "voice": assembled.voice,
    })


@router.post("/session.turn")
async def session_turn(
    body: TurnIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    await insert_call_turn(
        call_id=x_call_id, role=body.role, text=body.text, seq=body.seq,
    )
    return Envelope[dict](ok=True, data={"appended": True})


@router.post("/session.ended")
async def session_ended(
    body: EndedIn,
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    await finalize_call(
        call_id=x_call_id, ended_at=body.ended_at,
        duration_seconds=body.duration_seconds,
    )
    call = await get_call(x_call_id)
    if not call:
        return Envelope[dict](ok=False, error="call_not_found")
    await record_voice_debit(
        shop_id=call["shop_id"], call_id=x_call_id,
        duration_seconds=body.duration_seconds,
        tool_token_cost=body.tool_token_cost,
        tokens_per_second=settings.voice_kairo_tokens_per_second,
    )
    return Envelope[dict](ok=True, data={"finalized": True})