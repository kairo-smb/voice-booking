"""OpenAI Realtime SIP incoming-call webhook.

OpenAI fires `realtime.call.incoming` when a SIP call reaches our project. We
identify the shop (from the X-Shop-Id SIP header we set), assemble the
session (prompt + the 12 authz'd tools), and accept the call.

ponytail: webhook signature verified only when OPENAI_WEBHOOK_SECRET is set.
Wire mandatory verification once the signing secret is provisioned.
"""
from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from booking_engine.clients.openai_realtime import accept_sip_call
from booking_engine.config import Settings, get_settings
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.db.voice_config_queries import get_config, get_policy
from booking_engine.services.call_supervisor import maybe_supervise
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.identity_resolver import resolve_caller
from booking_engine.services.realtime_session import (
    build_accept_payload, caller_from_sip_headers, shop_id_from_sip_headers,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice/openai", tags=["voice-openai"])


@router.post("/incoming")
async def incoming(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    event = await request.json()
    if event.get("type") != "realtime.call.incoming":
        return {"status": "ignored"}

    data = event.get("data", {})
    call_id = data.get("call_id")
    headers = data.get("sip_headers", [])
    shop_id = shop_id_from_sip_headers(headers)
    if not shop_id and settings.sip_test_fallback_shop_id:
        # Raw SIP test call (e.g. a softphone dialing OpenAI directly, no
        # Twilio in the path to translate a header in) — only ever set on QA.
        shop_id = UUID(settings.sip_test_fallback_shop_id)
        logger.info("openai.incoming: no X-Shop-Id header, using test fallback shop %s", shop_id)
    if not call_id or not shop_id:
        logger.warning("openai.incoming: missing call_id/shop_id headers=%s", headers)
        return {"status": "unroutable"}

    config = await get_config(shop_id)
    if not config:
        logger.warning("openai.incoming: no config for shop %s", shop_id)
        return {"status": "no_config"}
    policy = await get_policy()
    if not policy:
        logger.warning("openai.incoming: no policy configured")
        return {"status": "no_config"}

    caller = caller_from_sip_headers(headers)
    resolution = await resolve_caller(shop_id=shop_id, caller_phone=caller)
    matched_id = (
        resolution.unique_match.customer_id if resolution.unique_match else None
    )
    db_call_id = await insert_call(
        shop_id=shop_id, caller_phone=caller, matched_customer_id=matched_id,
    )

    # Remote MCP: OpenAI calls our tool server directly with a per-call bearer
    # carrying our internal call/shop ids (distinct from OpenAI's SIP call_id).
    # Trailing slash is required: /mcp 307-redirects to /mcp/, and OpenAI's
    # Realtime MCP client does not re-POST the body on the redirect (verified in
    # fly logs: bare /mcp calls 307 and never complete). Point straight at /mcp/.
    mcp_url = f"{settings.public_base_url}/mcp/" if settings.public_base_url else None
    mcp_token = (
        mint_call_token(shop_id=shop_id, call_id=db_call_id,
                        secret=settings.openai_tool_secret)
        if mcp_url else None
    )
    payload = await build_accept_payload(
        config=config, policy=policy, resolution=resolution,
        model=settings.openai_realtime_model,
        mcp_server_url=mcp_url, mcp_token=mcp_token,
        enable_input_transcription=settings.call_supervisor_verbose_logging,
    )
    ok = await accept_sip_call(
        call_id=call_id, payload=payload, api_key=settings.openai_api_key,
    )
    if ok:
        # Own the call's control channel: greet + voice tool results. Gated by
        # ENABLE_CALL_SUPERVISOR; no-op when off. Fire-and-forget by design.
        maybe_supervise(call_id, settings)
    return {"status": "accepted" if ok else "accept_failed"}
