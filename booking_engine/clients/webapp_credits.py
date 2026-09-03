"""HTTP client for the webapp's AI-credit charge endpoint.

The webapp owns `business_app_core.ai_token_basket` and writes its ledger
(`ai_run_ledger`); this repo must not touch either. A voice call or SMS send
bills the shop by POSTing a **pre-converted credits amount** to the webapp's
charge-actual endpoint — the same single-phase charge the marketing engine
uses. The meter here has already computed the credits (voice: seconds × rate +
tool cost; SMS: 2× the Twilio price via `send_credits`), so we send `credits`,
never USD: the margin rule lives only in the webapp's `run-credits`, and a
second conversion must not appear on this side.

Never throws. A refusal (402 — the basket can't cover it), any other HTTP
error, or an unreachable webapp is logged loudly and returned as `ok: False`.
The caller refuses the charge and moves on; there is no local deduction
arithmetic to fall back to.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from booking_engine.config import Settings

logger = logging.getLogger(__name__)

_CHARGE_ACTUAL_PATH = "/api/v1/hair-salon/marketing/charge-actual"
_TIMEOUT_SECONDS = 10.0

# Run kinds this repo can charge. Values are the webapp's RunType vocabulary,
# which lands in ai_run_ledger.run_kind verbatim (voice/SMS are Twilio spend,
# not LLM spend, so they have no market_intel.usage_events sibling).
VOICE_CALL = "voice_call"
SMS_SEND = "sms_send"


async def charge_actual(
    *,
    shop_id: UUID,
    run_type: str,
    run_ref: str | None,
    credits: int,
    settings: Settings,
) -> bool:
    """Charge `credits` to `shop_id`'s basket for `run_ref`. True when collected.

    `run_type` is the wire value this repo can charge — `VOICE_CALL` or
    `SMS_SEND` (the webapp's RunType vocabulary, which it stores in
    `ai_run_ledger.run_kind`). `run_ref` makes the ledger row traceable back
    to the call/message that caused it.
    """
    base = settings.webapp_base_url.rstrip("/")
    secret = settings.market_intel_secret
    if not base or not secret:
        logger.error(
            "webapp_credits charge not attempted shop=%s run_type=%s run_ref=%s "
            "credits=%s: WEBAPP_BASE_URL/MARKET_INTEL_SECRET not configured",
            shop_id, run_type, run_ref, credits,
        )
        return False

    payload = {
        "shop_id": str(shop_id),
        "run_type": run_type,
        "credits": credits,
    }
    if run_ref is not None:
        payload["run_ref"] = run_ref

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}{_CHARGE_ACTUAL_PATH}",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.error(
            "webapp_credits charge unreachable shop=%s run_type=%s run_ref=%s "
            "credits=%s err=%s",
            shop_id, run_type, run_ref, credits, exc,
        )
        return False

    if resp.status_code == 200:
        return True

    # 402 = empty/insufficient basket. The webapp returns the amount it would
    # have needed in `required`; the charge is REFUSED, never forced. We log
    # it loudly and leave the bucket exactly as the webapp's locked
    # transaction left it — no draining to an arbitrary value.
    logger.error(
        "webapp_credits charge %s shop=%s run_type=%s run_ref=%s credits=%s "
        "status=%s body=%s",
        "REFUSED" if resp.status_code == 402 else "failed",
        shop_id, run_type, run_ref, credits, resp.status_code, resp.text[:300],
    )
    return False
