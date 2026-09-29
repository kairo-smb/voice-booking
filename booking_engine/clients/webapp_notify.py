"""HTTP client for the webapp's owner-facing email.

The mailbox lives on the webapp side — Resend, the rendered templates, the
shop's locale and the owner's address are all there, and duplicating any of it
here would be a second place for "who do we write to" to drift. This repo owns
the *facts*: `whatsapp.senders.token_expires_at` and the low-credit episode on
`shop_config.credit_low_notified_at`, and the hourly tick that notices either.

Same shared bearer as `webapp_credits` (`MARKET_INTEL_SECRET`). A second
secret for the same hop would be a second thing to rotate.

Never throws. Unreachable, refused, or unconfigured is logged and returned as
False (None for the credit notice, whose caller retries it — see
`whatsapp_credit_low`); the token reminder records the attempt either way,
because an hourly retry against a salon with no owner mailbox is noise, not
resilience — and the in-app banner still covers that salon.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from booking_engine.config import Settings

logger = logging.getLogger(__name__)

_TOKEN_EXPIRING_PATH = "/api/v1/hair-salon/whatsapp/token-expiring"
_CREDIT_LOW_PATH = "/api/v1/hair-salon/whatsapp/credit-low"
_TIMEOUT_SECONDS = 10.0


async def whatsapp_token_expiring(
    *, shop_id: UUID, days_left: int, phone_number: str | None, settings: Settings,
) -> bool:
    """Ask the webapp to email this salon's owner. True when it says it sent.

    `days_left` may be zero or negative: the token is already dead, the salon
    is not sending, and the reconnect is still the fix — the template says so
    in different words rather than the caller suppressing the mail.
    """
    data = await _post(
        "token_expiring", _TOKEN_EXPIRING_PATH, shop_id=shop_id,
        payload={"days_left": days_left, "phone_number": phone_number or ""},
        settings=settings,
    )
    return bool(data and data.get("sent"))


async def whatsapp_credit_low(
    *, shop_id: UUID, balance: int, threshold: int, settings: Settings,
) -> bool | None:
    """Ask the webapp to tell the owner the WhatsApp responder paused.

    Three answers, not two, because the caller's bookkeeping differs: True /
    False mean the webapp answered (sent, or a fact about the shop such as no
    owner mailbox — record the attempt, the episode is handled); **None** means
    it never answered (unconfigured, unreachable, refused) — leave the episode
    unstamped so the next tick tries again. Unlike the token reminder there is
    no cooldown to fall back on: a stamp here is the whole episode's one mail.
    """
    data = await _post(
        "credit_low", _CREDIT_LOW_PATH, shop_id=shop_id,
        payload={"balance": balance, "threshold": threshold},
        settings=settings,
    )
    return None if data is None else bool(data.get("sent"))


async def _post(
    kind: str, path: str, *, shop_id: UUID, payload: dict, settings: Settings,
) -> dict | None:
    """POST one notice. The response's `data`, or None when there was none."""
    base = settings.webapp_base_url.rstrip("/")
    secret = settings.market_intel_secret
    if not base or not secret:
        logger.error(
            "webapp_notify %s not attempted shop=%s: "
            "WEBAPP_BASE_URL/MARKET_INTEL_SECRET not configured", kind, shop_id,
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}{path}",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                json={"shop_id": str(shop_id), **payload},
            )
    except httpx.HTTPError as exc:
        logger.error("webapp_notify %s unreachable shop=%s err=%s", kind, shop_id, exc)
        return None

    if resp.status_code != 200:
        logger.error("webapp_notify %s refused shop=%s status=%s body=%s",
                     kind, shop_id, resp.status_code, resp.text[:200])
        return None

    body = resp.json() if resp.content else {}
    data = body.get("data") or {}
    if not data.get("sent"):
        # A shop with no owner email, or Resend refusing. Worth knowing about —
        # that salon's only remaining warning is a banner it may never open.
        logger.warning("webapp_notify %s not sent shop=%s reason=%s",
                       kind, shop_id, data.get("reason"))
    return data
