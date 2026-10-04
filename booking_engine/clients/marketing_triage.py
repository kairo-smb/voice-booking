"""Classify an inbound WhatsApp message via the marketing-engine gateway.

The classifier itself lives in the marketing-engine repo (the LLM gateway);
this is the thin HTTP client that calls it. A customer's inbound message has
to be *named* — booking, cancel, complaint, and so on — before anything can
route it.

Every failure returns `None` — unconfigured, missing secret, 402 (the salon
has no AI credit and the engine refused to spend on a shop that cannot pay),
engine down, timeout, malformed JSON, a `data` that isn't the dict shape a
verdict must be. `None` means "unrouted", which means a human looks at the
thread. There is no path here that invents or guesses a verdict, because a
wrong verdict routes a real customer to the wrong handler and nobody ever
finds out — same fail-closed posture as `webapp_credits.py`'s 402 handling
and `whatsapp_onboarding.py`'s template gate.

`classify()` never raises. It is awaited from a fire-and-forget background
task (an inbound webhook handler); an uncaught exception there is a silently
dropped customer message.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from booking_engine.config import Settings

logger = logging.getLogger(__name__)

_TRIAGE_PATH = "/whatsapp/triage"
_TIMEOUT_SECONDS = 15.0


async def classify(*, shop_id: UUID, text: str, settings: Settings) -> dict | None:
    """Classify `text` for `shop_id`. Returns None on any failure — never guess."""
    try:
        return await _classify(shop_id=shop_id, text=text, settings=settings)
    except Exception:  # noqa: BLE001 — every failure here is the same refusal
        logger.exception("whatsapp.triage_unexpected_error shop=%s", shop_id)
        return None


async def _classify(*, shop_id: UUID, text: str, settings: Settings) -> dict | None:
    base = (settings.market_intel_api_url or "").rstrip("/")
    secret = settings.market_intel_secret or ""
    if not base or not secret:
        logger.warning(
            "whatsapp.triage_unconfigured shop=%s: MARKET_INTEL_API_URL/"
            "MARKET_INTEL_SECRET not configured",
            shop_id,
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}{_TRIAGE_PATH}",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                json={"shop_id": str(shop_id), "text": text},
            )
    except httpx.HTTPError as exc:
        logger.error("whatsapp.triage_unreachable shop=%s err=%s", shop_id, exc)
        return None

    if resp.status_code == 402:
        # Expected traffic, not an incident: the salon has no AI credit and
        # the engine refused to classify rather than spend on a shop that
        # can't pay. Same posture as webapp_credits' 402 handling — logged
        # quietly, not at exception volume, or an out-of-credit salon would
        # fill the logs with stack traces indistinguishable from a real
        # outage.
        logger.info("whatsapp.triage_refused_no_credit shop=%s", shop_id)
        return None

    if resp.status_code != 200:
        logger.error(
            "whatsapp.triage_failed shop=%s status=%s body=%s",
            shop_id, resp.status_code, resp.text[:300],
        )
        return None

    try:
        body = resp.json()
    except ValueError:
        logger.error("whatsapp.triage_malformed_json shop=%s", shop_id)
        return None

    verdict = body.get("data") if isinstance(body, dict) else None
    if not isinstance(verdict, dict):
        logger.error(
            "whatsapp.triage_malformed_verdict shop=%s data=%r", shop_id, verdict,
        )
        return None

    return verdict
