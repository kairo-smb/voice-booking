"""Meta WhatsApp Cloud API — Graph client.

Replaced the Twilio BSP client on 2026-08-24; see CLAUDE.md for why Twilio
could not hold a salon's own WABA at all.

Every call into a salon's WABA is authenticated with **that salon's** business
token, obtained once from Embedded Signup's code exchange. There is no shared
parent credential the way a Twilio subaccount had one: a token here is the
complete authority over one customer's WhatsApp, so it is passed explicitly at
every call site rather than defaulted.

Error handling is deliberately shallow. Graph returns
`{"error": {"code": 131050, "message": …, "error_data": {"details": …}}}` with
a 4xx, and the caller (`whatsapp_send`) needs the numeric code to tell an
opt-out from a frequency cap from a genuine failure — so `MetaError` carries
it rather than being flattened into a string.
"""
from __future__ import annotations

from dataclasses import dataclass

from httpx import AsyncClient, HTTPStatusError

# ponytail: pinned, not discovered. Graph is versioned and changes shape across
# versions; bump deliberately when Meta's changelog says to.
GRAPH = "https://graph.facebook.com/v25.0"
_TIMEOUT = 30.0


class MetaError(Exception):
    """A Graph API error, with the numeric code the send path branches on."""

    def __init__(self, code: int | None, message: str, subcode: int | None = None):
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code
        self.subcode = subcode
        self.message = message


async def _request(
    method: str, path: str, *, token: str,
    json_body: dict | None = None, params: dict | None = None,
) -> dict:
    async with AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.request(
            method,
            f"{GRAPH}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            params=params,
        )
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        try:
            response.raise_for_status()
        except HTTPStatusError as exc:
            err = body.get("error") or {}
            raise MetaError(
                code=err.get("code"),
                message=err.get("message") or str(exc),
                subcode=err.get("error_subcode"),
            ) from exc
        return body


# ------------------------------------------------------------------ onboarding

async def exchange_code(*, code: str, app_id: str, app_secret: str) -> str:
    """Turn Embedded Signup's one-time code into the salon's business token.

    This is the only call that uses Kairo's own app credentials rather than a
    customer token — it is how a customer token comes into existence.
    """
    async with AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
            },
        )
        body = response.json() if response.content else {}
        if response.status_code >= 400 or not body.get("access_token"):
            err = (body.get("error") or {})
            raise MetaError(err.get("code"), err.get("message") or "code_exchange_failed")
        return body["access_token"]


async def subscribe_app(*, waba_id: str, token: str) -> None:
    """Subscribe our app to this WABA's webhooks.

    Without it we receive nothing at all for this customer — no delivery
    status, no template verdicts, no opt-outs — while every send still
    succeeds. That silent-but-broken shape is why it runs before anything else
    in onboarding.
    """
    await _request("POST", f"{waba_id}/subscribed_apps", token=token)


@dataclass(frozen=True)
class PhoneNumber:
    id: str
    display_phone_number: str
    verified_name: str
    quality_rating: str | None
    # Volume: business-initiated conversations per ROLLING 24h ('TIER_1K', …).
    messaging_limit: str | None
    # Rate: messages per second ('STANDARD' | 'HIGH'). A different ceiling
    # from the one above, with a different consequence — conflating them is
    # how migration 15 ended up storing throughput in `messaging_limit`.
    throughput_level: str | None
    platform_type: str | None
    is_on_biz_app: bool


async def get_phone_number(*, phone_number_id: str, token: str) -> PhoneNumber:
    """Read a business phone number back: coexistence status and both ceilings.

    `is_on_biz_app` / `platform_type` are Meta's own answer to "did the salon
    keep their WhatsApp Business App?" — the single fact that distinguishes a
    coexistence onboarding from an ordinary one, and the reason we don't have
    to trust what the popup told the browser.

    `messaging_limit_tier` and `throughput` are what `meta_limits.py` enforces
    against. Both are read on every sweep, not just at onboarding: Meta
    re-evaluates the tier every 6 hours and a sender can move in either
    direction.
    """
    body = await _request(
        "GET", phone_number_id, token=token,
        params={"fields": "id,display_phone_number,verified_name,quality_rating,"
                          "messaging_limit_tier,throughput,platform_type,"
                          "is_on_biz_app"},
    )
    return PhoneNumber(
        id=body.get("id", phone_number_id),
        display_phone_number=body.get("display_phone_number", ""),
        verified_name=body.get("verified_name", ""),
        quality_rating=body.get("quality_rating"),
        messaging_limit=body.get("messaging_limit_tier"),
        throughput_level=(body.get("throughput") or {}).get("level"),
        platform_type=body.get("platform_type"),
        is_on_biz_app=bool(body.get("is_on_biz_app")),
    )


async def register_phone_number(*, phone_number_id: str, pin: str, token: str) -> None:
    """Enable a number for Cloud API.

    Skipped for coexistence — that number is already registered, and Meta's
    own guidance is not to call this. Kept for the `source='new'` branch,
    where nothing else performs registration: neither WhatsApp Manager nor the
    App Dashboard do it, only this endpoint.
    """
    await _request(
        "POST", f"{phone_number_id}/register", token=token,
        json_body={"messaging_product": "whatsapp", "pin": pin},
    )


# ------------------------------------------------------------------- templates

async def create_template(
    *, waba_id: str, token: str, name: str, language: str,
    category: str, body_text: str, sample_variables: dict[str, str],
) -> tuple[str, str]:
    """Create a template on the *salon's* WABA. Returns (id, status).

    This single call is what Twilio could not do at all — a WABA it doesn't
    own is closed to it — and is therefore the whole reason for this
    migration. `example` is not optional: Meta rejects a body containing
    variables without one, and every useful marketing template opens with
    "Ciao {{1}}".
    """
    ordered = [sample_variables[str(i)] for i in range(1, len(sample_variables) + 1)]
    body = await _request(
        "POST", f"{waba_id}/message_templates", token=token,
        json_body={
            "name": name,
            "language": language,
            "category": category,
            "components": [{
                "type": "BODY",
                "text": body_text,
                **({"example": {"body_text": [ordered]}} if ordered else {}),
            }],
        },
    )
    return body.get("id", ""), (body.get("status") or "PENDING").lower()


@dataclass(frozen=True)
class TemplateStatus:
    status: str
    rejection_reason: str | None


async def fetch_template(*, waba_id: str, name: str, token: str) -> TemplateStatus | None:
    """Meta's current verdict on one template, by name.

    Only used by the tick's reconciler: verdicts normally arrive as
    `message_template_status_update` webhooks within minutes. A missed webhook
    would otherwise leave a template `pending` forever and silently block
    every send for that shop.
    """
    body = await _request(
        "GET", f"{waba_id}/message_templates", token=token,
        params={"name": name, "fields": "name,status,rejected_reason", "limit": 5},
    )
    for row in body.get("data", []):
        if row.get("name") == name:
            return TemplateStatus(
                status=(row.get("status") or "pending").lower(),
                rejection_reason=row.get("rejected_reason") or None,
            )
    return None


# ---------------------------------------------------------------------- send

async def send_template(
    *, phone_number_id: str, token: str, to: str,
    name: str, language: str, variables: dict[str, str],
) -> str:
    """Send one approved template. Returns Meta's `wamid`.

    No price comes back, here or on the status webhook — Meta bills the salon
    directly and never reports an amount to us. `outbound_messages.price_usd`
    is our own estimate, written at send time.
    """
    ordered = [variables[k] for k in sorted(variables, key=int)]
    template: dict = {"name": name, "language": {"code": language}}
    if ordered:
        template["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": v} for v in ordered],
        }]
    body = await _request(
        "POST", f"{phone_number_id}/messages", token=token,
        json_body={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": template,
        },
    )
    messages = body.get("messages") or [{}]
    return messages[0].get("id", "")
