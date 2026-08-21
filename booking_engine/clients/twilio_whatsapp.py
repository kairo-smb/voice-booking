"""Twilio WhatsApp: subaccounts, Senders API, Content (template) API.

Three Twilio APIs, one module, because they are only ever used together —
onboarding a salon touches all three in sequence.

Auth note that is easy to get wrong: a subaccount is addressed with the
*subaccount* SID as the basic-auth username and the *parent's* auth token as
the password. So there is no per-salon secret to store anywhere; the
`subaccount_sid` column plus the one `TWILIO_AUTH_TOKEN` we already have is
the complete credential set.

See CLAUDE.md's WhatsApp entry for why each salon needs its own subaccount.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from httpx import AsyncClient, BasicAuth

_ACCOUNTS = "https://api.twilio.com/2010-04-01"
_SENDERS = "https://messaging.twilio.com/v2/Channels/Senders"
_CONTENT = "https://content.twilio.com/v1/Content"
_TIMEOUT = 30.0


def _auth(account_sid: str, auth_token: str) -> BasicAuth:
    return BasicAuth(account_sid, auth_token)


async def _request(method: str, url: str, *, account_sid: str, auth_token: str,
                   json_body: dict | None = None, data: dict | None = None) -> dict:
    async with AsyncClient(auth=_auth(account_sid, auth_token), timeout=_TIMEOUT) as c:
        r = await c.request(method, url, json=json_body, data=data)
        r.raise_for_status()
        return r.json() if r.content else {}


# ---------------------------------------------------------------- subaccounts

@dataclass(frozen=True)
class Subaccount:
    sid: str
    auth_token: str


async def create_subaccount(*, friendly_name: str,
                            account_sid: str, auth_token: str) -> Subaccount:
    """Create a subaccount to hold one salon's WABA.

    The subaccount's own auth token is returned here and *only* here — Twilio
    exposes it on creation and on fetch, and it is needed to validate the
    webhooks that subaccount will send us. API calls into the subaccount don't
    need it (parent token + subaccount SID works for those).
    """
    body = await _request(
        "POST", f"{_ACCOUNTS}/Accounts.json",
        account_sid=account_sid, auth_token=auth_token,
        data={"FriendlyName": friendly_name},
    )
    return Subaccount(sid=body["sid"], auth_token=body.get("auth_token", ""))


async def close_subaccount(*, subaccount_sid: str,
                           account_sid: str, auth_token: str) -> None:
    """Permanently close a subaccount. Irreversible on Twilio's side."""
    await _request(
        "POST", f"{_ACCOUNTS}/Accounts/{subaccount_sid}.json",
        account_sid=account_sid, auth_token=auth_token,
        data={"Status": "closed"},
    )


# --------------------------------------------------------------- OTP capture

async def set_sms_webhook(*, number_sid: str, sms_url: str,
                          account_sid: str, auth_token: str) -> None:
    """Point a number's inbound-SMS webhook at us (or clear it with "").

    Used only to catch Meta's ownership-verification OTP on a Kairo-owned
    number and then unset it again. This is not a reintroduction of STOP
    handling (removed 2026-08-15) — nothing here parses message content
    beyond a 6-digit code, and the hook is removed once the sender is online.
    """
    await _request(
        "POST", f"{_ACCOUNTS}/Accounts/{account_sid}/IncomingPhoneNumbers/{number_sid}.json",
        account_sid=account_sid, auth_token=auth_token,
        data={"SmsUrl": sms_url, "SmsMethod": "POST"},
    )


# ------------------------------------------------------------------- senders

@dataclass(frozen=True)
class Sender:
    sid: str
    status: str                 # CREATING | VERIFYING | ONLINE | OFFLINE | …
    phone_number: str
    quality_rating: str | None
    messaging_limit: str | None
    offline_reason: str | None


def _to_sender(body: dict) -> Sender:
    props = body.get("properties") or {}
    offline = body.get("offline_reasons") or None
    return Sender(
        sid=body.get("sid", ""),
        status=body.get("status", ""),
        phone_number=(body.get("sender_id") or "").removeprefix("whatsapp:"),
        quality_rating=props.get("quality_rating"),
        messaging_limit=props.get("messaging_limit"),
        offline_reason=json.dumps(offline)[:500] if offline else None,
    )


async def register_sender(
    *,
    subaccount_sid: str,
    auth_token: str,
    phone_number: str,
    waba_id: str,
    display_name: str,
    status_callback_url: str,
    callback_url: str,
) -> Sender:
    """Register a phone number as this subaccount's WhatsApp sender.

    `account_type: ISVSubAccount` is what tells Twilio this sender belongs to
    an ISV's customer rather than to the ISV — the same audited distinction
    that forces a regulatory bundle per salon (CLAUDE.md, 2026-08-14).

    Verification is always `sms` and always completed manually via
    `verify_sender`: the number is never owned by the subaccount making this
    request (it lives in the parent account, or with the salon), so Twilio's
    automatic path doesn't apply either way.
    """
    body = await _request(
        "POST", _SENDERS,
        account_sid=subaccount_sid, auth_token=auth_token,
        json_body={
            "sender_id": f"whatsapp:{phone_number}",
            "configuration": {
                "waba_id": waba_id,
                "verification_method": "sms",
                "account_type": "ISVSubAccount",
            },
            "webhook": {
                "callback_url": callback_url,
                "callback_method": "POST",
                "status_callback_url": status_callback_url,
                "status_callback_method": "POST",
            },
            "profile": {"name": display_name},
        },
    )
    return _to_sender(body)


async def verify_sender(*, sender_sid: str, code: str,
                        subaccount_sid: str, auth_token: str) -> Sender:
    """Submit Meta's ownership OTP for a sender that is VERIFYING."""
    body = await _request(
        "POST", f"{_SENDERS}/{sender_sid}",
        account_sid=subaccount_sid, auth_token=auth_token,
        json_body={"configuration": {"verification_code": code}},
    )
    return _to_sender(body)


async def fetch_sender(*, sender_sid: str, subaccount_sid: str,
                       auth_token: str) -> Sender:
    body = await _request(
        "GET", f"{_SENDERS}/{sender_sid}",
        account_sid=subaccount_sid, auth_token=auth_token,
    )
    return _to_sender(body)


# ------------------------------------------------------------------ templates

async def create_template(
    *,
    subaccount_sid: str,
    auth_token: str,
    friendly_name: str,
    language: str,
    body_text: str,
    sample_variables: dict[str, str],
) -> str:
    """Create a Content template in the salon's subaccount. Returns the HX SID.

    `sample_variables` is not optional in practice: Meta rejects a template
    whose body starts or ends with a variable unless a sample is supplied,
    and every useful marketing template starts with "Ciao {{1}}".
    """
    body = await _request(
        "POST", _CONTENT,
        account_sid=subaccount_sid, auth_token=auth_token,
        json_body={
            "friendly_name": friendly_name,
            "language": language,
            "variables": sample_variables,
            "types": {"twilio/text": {"body": body_text}},
        },
    )
    return body["sid"]


async def submit_for_approval(*, content_sid: str, name: str, category: str,
                              subaccount_sid: str, auth_token: str) -> str:
    """Submit a template to Meta. Returns its initial approval status."""
    body = await _request(
        "POST", f"{_CONTENT}/{content_sid}/ApprovalRequests/whatsapp",
        account_sid=subaccount_sid, auth_token=auth_token,
        json_body={"name": name, "category": category},
    )
    return body.get("status", "unsubmitted")


@dataclass(frozen=True)
class Approval:
    status: str
    rejection_reason: str | None


async def fetch_approval(*, content_sid: str, subaccount_sid: str,
                         auth_token: str) -> Approval:
    body = await _request(
        "GET", f"{_CONTENT}/{content_sid}/ApprovalRequests",
        account_sid=subaccount_sid, auth_token=auth_token,
    )
    wa = (body.get("whatsapp") or {})
    return Approval(
        status=wa.get("status", "unsubmitted"),
        rejection_reason=wa.get("rejection_reason") or None,
    )
