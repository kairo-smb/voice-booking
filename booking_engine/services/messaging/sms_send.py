"""Send one marketing SMS to one customer.

Order matters and is a trust boundary, not a style choice: consent is re-checked
here even though the webapp checks it twice, because this is the last code that
runs before a named individual receives marketing. The balance is checked before
the provider call and debited only after it accepts: refusing a send we can't
bill, without charging for one the provider rejected. See
docs/messaging-design.md §6.3.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from twilio.rest import Client

from booking_engine.config import Settings
from booking_engine.db import sms_queries
from booking_engine.db import token_basket_queries as tbq
from booking_engine.services.messaging.gsm7 import encode_info, sanitize
from booking_engine.services.messaging.send_credits import send_credits
from booking_engine.services.phone_normalize import normalize_e164

logger = logging.getLogger(__name__)

# Twilio list price for Italy, used only to pre-charge. The status callback
# writes the real price; this is never the billed figure of record.
_ESTIMATED_USD_PER_SEGMENT = 0.093


@dataclass(frozen=True)
class TwilioResult:
    sid: str
    price_usd: float | None


@dataclass(frozen=True)
class SendResult:
    ok: bool
    reason: str            # 'sent' | why it didn't go
    message_id: UUID | None = None
    segments: int = 0
    credits: int = 0


def _has_active_consent(customer: dict) -> bool:
    """Mirrors the webapp's hasActiveMarketingConsent() exactly.

    The only suppression rule: opt-out is handled in-store (a staff member
    clears marketing consent in the app), not via an in-message STOP reply —
    see CLAUDE.md's STOP-removal entry.
    """
    return (
        bool(customer.get("marketing_consent"))
        and customer.get("marketing_consent_granted_at") is not None
        and customer.get("marketing_consent_withdrawn_at") is None
    )


def _twilio_send(*, to: str, from_: str, body: str,
                 account_sid: str, auth_token: str,
                 status_callback: str | None = None) -> TwilioResult:
    """Blocking Twilio call. Wrapped in a thread by the caller."""
    client = Client(account_sid, auth_token)
    msg = client.messages.create(
        to=to, from_=from_, body=body, status_callback=status_callback,
    )
    return TwilioResult(
        sid=msg.sid,
        price_usd=abs(float(msg.price)) if getattr(msg, "price", None) else None,
    )


async def send_marketing_sms(
    *,
    shop_id: UUID,
    customer_id: UUID,
    body: str,
    settings: Settings,
    account_sid: str = "",
    auth_token: str = "",
    public_base_url: str = "",
) -> SendResult:
    """Gate, build, bill, send. Every refusal is persisted, never silent.

    `settings` carries the webapp charge-actual config (WEBAPP_BASE_URL +
    MARKET_INTEL_SECRET) used to bill the send after Twilio accepts it — the
    basket deduction itself lives on the webapp side, never here.
    """
    sender = await sms_queries.get_shop_sender_number(shop_id)
    if not sender:
        return SendResult(ok=False, reason="no_sender_number")

    customer = await sms_queries.get_customer_for_send(shop_id, customer_id)
    if not customer:
        return SendResult(ok=False, reason="customer_not_found")

    # customers.phone_normalized is a GENERATED column: regexp_replace(phone,'\D','')
    # — digits only, no leading '+'. Handing that straight to Twilio sends to a
    # malformed destination, so always re-normalise to E.164 here.
    phone = (
        normalize_e164(customer.get("phone"))
        or normalize_e164(customer.get("phone_normalized"))
    )
    text = sanitize(body.strip())
    info = encode_info(text)
    # Without this Twilio never calls POST /sms/webhook/status, so our row
    # stays 'sent' forever and price_usd stays NULL even though Twilio has
    # the real price. Empty when public_base_url isn't configured (local/test).
    status_callback = (
        f"{public_base_url}/api/v1/sms/webhook/status" if public_base_url else None
    )

    async def _suppress(reason: str) -> SendResult:
        # Recorded, not dropped: "why did Giulia not get it?" must be answerable.
        mid = await sms_queries.insert_outbound(
            shop_id=shop_id, customer_id=customer_id, to_phone=phone or "",
            from_number=sender, body=text, segments=info.segments,
            encoding=info.encoding, status="suppressed", suppressed_reason=reason,
        )
        return SendResult(ok=False, reason=reason, message_id=mid)

    if not phone:
        return await _suppress("no_phone")
    if not _has_active_consent(customer):
        return await _suppress("no_consent")

    message_id = await sms_queries.insert_outbound(
        shop_id=shop_id, customer_id=customer_id, to_phone=phone,
        from_number=sender, body=text, segments=info.segments,
        encoding=info.encoding, status="queued",
    )

    # Estimated from list price; the status webhook reconciles with the real one.
    credits = send_credits(_ESTIMATED_USD_PER_SEGMENT * info.segments)

    # Check the balance now, debit only after the provider accepts. Debiting
    # first meant a Twilio rejection left the shop paying for a message that was
    # never sent, on a call that cost us nothing — charging for that is worse
    # than the narrow race this ordering accepts.
    if await tbq.get_balance(shop_id) < credits:
        await sms_queries.mark_failed(message_id=message_id, error_code="insufficient_credits")
        return SendResult(ok=False, reason="insufficient_credits", message_id=message_id)

    try:
        result = await asyncio.to_thread(
            _twilio_send, to=phone, from_=sender, body=text,
            account_sid=account_sid, auth_token=auth_token,
            status_callback=status_callback,
        )
    except Exception as exc:  # noqa: BLE001 — provider errors are data, not crashes
        await sms_queries.mark_failed(message_id=message_id, error_code=str(exc)[:200])
        return SendResult(ok=False, reason="provider_error", message_id=message_id)

    # ponytail: the message is already gone, so a refused charge here can only be
    # logged, not undone. Only reachable if the basket drained between the check
    # above and now — owner-triggered sends are effectively serial. The webapp's
    # charge is a single locked transaction, so the refusal is authoritative.
    if not await tbq.try_debit_for_message(
        shop_id=shop_id, credits=credits, sms_message_id=message_id,
        settings=settings,
    ):
        logger.warning(
            "sms.unbilled_send shop=%s message=%s credits=%s", shop_id, message_id, credits
        )

    await sms_queries.mark_sent(
        message_id=message_id, provider_sid=result.sid,
        price_usd=result.price_usd, credits=credits,
    )
    return SendResult(
        ok=True, reason="sent", message_id=message_id,
        segments=info.segments, credits=credits,
    )
