"""Number health semaphore — periodic green/red check for provisioned DIDs.

Design rule: a Twilio outage must not repaint every salon red. Only a
definite negative answer (the number is genuinely missing, or its webhooks
have drifted off our base URL) changes the light. An inconclusive probe
(we could not reach Twilio, or couldn't otherwise tell) leaves the previous
verdict standing and only records that we tried — see decide_health's
docstring and number_request_queries.set_health, which encodes this same
contract at the DB layer (status=None -> health_checked_at stamped,
health_status untouched).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from twilio.base.exceptions import TwilioRestException

from booking_engine.clients.twilio_numbers import fetch_number
from booking_engine.config import Settings
from booking_engine.db.number_request_queries import list_provisioned_numbers, set_health

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthProbe:
    found: bool
    voice_url: str
    reachable: bool


def decide_health(probe: HealthProbe, *, base_url: str) -> tuple[str | None, str | None]:
    """Turn one probe into a (status, detail) verdict.

    status is one of "green", "red", or None. None means "no verdict" —
    the probe was inconclusive and the caller (set_health) must leave the
    previously stored health_status alone rather than overwrite it. This is
    the mechanism that keeps a transient Twilio outage from repainting every
    salon's number red: only a probe that can *prove* something is wrong
    (a confirmed 404, or a webhook pointing somewhere other than us) is
    allowed to flip the light. Everything else — including "we couldn't even
    reach Twilio to ask" — is treated as no information, not bad news.

    Checked in order:
    1. reachable=False first: an unreachable probe cannot prove absence, so
       it must not be allowed to fall through to the "missing" check below.
    2. found=False: Twilio returned a confirmed 404 — the number is gone.
    3. webhook drift: found, but voice_url doesn't start with our own
       base_url. sms_url is deliberately not checked — there is no inbound
       SMS handler any more (STOP handling was removed; see CLAUDE.md), so
       there is nothing for sms_url to correctly point at.
    4. otherwise green.
    """
    if not probe.reachable:
        return None, "provider_unreachable"
    if not probe.found:
        return "red", "number_missing"
    if not probe.voice_url.startswith(base_url):
        return "red", "webhook_drift"
    return "green", None


async def check_all(*, settings: Settings) -> dict:
    """Sweep every provisioned number, probe Twilio, and record a verdict.

    One shop's failure (a bug in our own probing code, not just a Twilio
    error — those are already handled by decide_health) must not abort the
    whole sweep, so the per-shop body is wrapped in its own try/except and
    counted as inconclusive rather than raised.
    """
    rows = await list_provisioned_numbers()
    counts = {"checked": 0, "green": 0, "red": 0, "inconclusive": 0}

    for row in rows:
        shop_id = row["shop_id"]
        sid = row["kairo_number_sid"]
        counts["checked"] += 1
        try:
            try:
                result = await asyncio.to_thread(
                    fetch_number,
                    sid=sid,
                    account_sid=settings.twilio_account_sid,
                    auth_token=settings.twilio_auth_token,
                )
                probe = HealthProbe(
                    found=True,
                    voice_url=result.voice_url,
                    reachable=True,
                )
            except TwilioRestException as exc:
                if exc.status == 404:
                    probe = HealthProbe(found=False, voice_url="", reachable=True)
                else:
                    probe = HealthProbe(found=False, voice_url="", reachable=False)

            status, detail = decide_health(probe, base_url=settings.public_base_url)
        except Exception:
            logger.exception("number_health.check_failed shop_id=%s", shop_id)
            status, detail = None, "check_failed"

        if status == "green":
            counts["green"] += 1
        elif status == "red":
            counts["red"] += 1
        else:
            counts["inconclusive"] += 1

        await set_health(shop_id=shop_id, status=status, detail=detail)

    return counts
