"""HTTP client for the webapp's owner-facing email.

The mailbox lives on the webapp side — Resend, the rendered templates, the
shop's locale and the owner's address are all there, and duplicating any of it
here would be a second place for "who do we write to" to drift. This repo owns
the *dates*: it holds `whatsapp.senders.token_expires_at` and the hourly tick
that notices one coming due.

Same shared bearer as `webapp_credits` (`MARKET_INTEL_SECRET`). A second
secret for the same hop would be a second thing to rotate.

Never throws. Unreachable, refused, or unconfigured is logged and returned as
False; the caller records the attempt either way, because an hourly retry
against a salon with no owner mailbox is noise, not resilience — and the
in-app banner still covers that salon.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from booking_engine.config import Settings

logger = logging.getLogger(__name__)

_TOKEN_EXPIRING_PATH = "/api/v1/hair-salon/whatsapp/token-expiring"
_TIMEOUT_SECONDS = 10.0


async def whatsapp_token_expiring(
    *, shop_id: UUID, days_left: int, phone_number: str | None, settings: Settings,
) -> bool:
    """Ask the webapp to email this salon's owner. True when it says it sent.

    `days_left` may be zero or negative: the token is already dead, the salon
    is not sending, and the reconnect is still the fix — the template says so
    in different words rather than the caller suppressing the mail.
    """
    base = settings.webapp_base_url.rstrip("/")
    secret = settings.market_intel_secret
    if not base or not secret:
        logger.error(
            "webapp_notify token_expiring not attempted shop=%s days_left=%s: "
            "WEBAPP_BASE_URL/MARKET_INTEL_SECRET not configured",
            shop_id, days_left,
        )
        return False

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}{_TOKEN_EXPIRING_PATH}",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                json={
                    "shop_id": str(shop_id),
                    "days_left": days_left,
                    "phone_number": phone_number or "",
                },
            )
    except httpx.HTTPError as exc:
        logger.error("webapp_notify token_expiring unreachable shop=%s err=%s",
                     shop_id, exc)
        return False

    if resp.status_code != 200:
        logger.error("webapp_notify token_expiring refused shop=%s status=%s body=%s",
                     shop_id, resp.status_code, resp.text[:200])
        return False

    body = resp.json() if resp.content else {}
    sent = bool((body.get("data") or {}).get("sent"))
    if not sent:
        # A shop with no owner email, or Resend refusing. Worth knowing about —
        # that salon's only remaining warning is a banner it may never open.
        logger.warning("webapp_notify token_expiring not sent shop=%s reason=%s",
                       shop_id, (body.get("data") or {}).get("reason"))
    return sent
