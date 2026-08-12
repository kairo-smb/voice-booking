"""Send one marketing SMS to one customer.

Order matters and is a trust boundary, not a style choice: consent is re-checked
here even though the webapp checks it twice, because this is the last code that
runs before a named individual receives marketing. Credits are debited BEFORE
the provider call — a send we can't bill must not happen, and an unbilled send
is worse than a refunded one. See docs/messaging-design.md §6.3.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from twilio.rest import Client

from booking_engine.db import sms_queries
from booking_engine.db import token_basket_queries as tbq
from booking_engine.services.messaging.gsm7 import encode_info, sanitize
from booking_engine.services.messaging.send_credits import send_credits
from booking_engine.services.phone_normalize import normalize_e164

# Legally required in every marketing message (Garante). Appended server-side,
# never left to the LLM that wrote the body.
OPT_OUT_FOOTER = " Rispondi STOP per non ricevere piu'."

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
    """Mirrors the webapp's hasActiveMarketingConsent() exactly."""
    return (
        bool(customer.get("marketing_consent"))
        and customer.get("marketing_consent_granted_at") is not None
        and customer.get("marketing_consent_withdrawn_at") is None
    )


def _twilio_send(*, to: str, from_: str, body: str,
                 account_sid: str, auth_token: str) -> TwilioResult:
    """Blocking Twilio call. Wrapped in a thread by the caller."""
    client = Client(account_sid, auth_token)
    msg = client.messages.create(to=to, from_=from_, body=body)
    return TwilioResult(
        sid=msg.sid,
        price_usd=abs(float(msg.price)) if getattr(msg, "price", None) else None,
    )


async def send_marketing_sms(
    *,
    shop_id: UUID,
    customer_id: UUID,
    body: str,
    account_sid: str = "",
    auth_token: str = "",
) -> SendResult:
    """Gate, build, bill, send. Every refusal is persisted, never silent."""
    sender = await sms_queries.get_shop_sender_number(shop_id)
    if not sender:
        return SendResult(ok=False, reason="no_sender_number")

    customer = await sms_queries.get_customer_for_send(shop_id, customer_id)
    if not customer:
        return SendResult(ok=False, reason="customer_not_found")

    phone = customer.get("phone_normalized") or normalize_e164(customer.get("phone"))
    text = sanitize(body.strip()) + OPT_OUT_FOOTER
    info = encode_info(text)

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
    if await sms_queries.is_opted_out(shop_id, phone):
        return await _suppress("opted_out")

    message_id = await sms_queries.insert_outbound(
        shop_id=shop_id, customer_id=customer_id, to_phone=phone,
        from_number=sender, body=text, segments=info.segments,
        encoding=info.encoding, status="queued",
    )

    # Estimated from list price; the status webhook reconciles with the real one.
    credits = send_credits(_ESTIMATED_USD_PER_SEGMENT * info.segments)
    if not await tbq.try_debit_for_message(
        shop_id=shop_id, credits=credits, sms_message_id=message_id
    ):
        await sms_queries.mark_failed(message_id=message_id, error_code="insufficient_credits")
        return SendResult(ok=False, reason="insufficient_credits", message_id=message_id)

    try:
        result = await asyncio.to_thread(
            _twilio_send, to=phone, from_=sender, body=text,
            account_sid=account_sid, auth_token=auth_token,
        )
    except Exception as exc:  # noqa: BLE001 — provider errors are data, not crashes
        await sms_queries.mark_failed(message_id=message_id, error_code=str(exc)[:200])
        return SendResult(ok=False, reason="provider_error", message_id=message_id)

    await sms_queries.mark_sent(
        message_id=message_id, provider_sid=result.sid,
        price_usd=result.price_usd, credits=credits,
    )
    return SendResult(
        ok=True, reason="sent", message_id=message_id,
        segments=info.segments, credits=credits,
    )
