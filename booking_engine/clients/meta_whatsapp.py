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
GRAPH = "https://graph.facebook.com/v26.0"
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
    # The BODY text Meta currently holds. The propagation gate compares it
    # against the catalogue: "approved" alone answers a question about a *name*,
    # and a name says nothing about which version of the copy was approved.
    body: str = ""


async def fetch_template(*, waba_id: str, name: str, token: str) -> TemplateStatus | None:
    """Meta's current verdict on one template, by name — with the body it ruled on.

    Two callers: the tick's reconciler (verdicts normally arrive as
    `message_template_status_update` webhooks within minutes, and a missed one
    would leave a template `pending` forever, blocking every send for that shop
    in silence), and the propagation gate, which asks the same question of
    *Kairo's* WABA before pushing anything into a customer's.
    """
    body = await _request(
        "GET", f"{waba_id}/message_templates", token=token,
        params={"name": name,
                "fields": "name,status,rejected_reason,components", "limit": 5},
    )
    for row in body.get("data", []):
        if row.get("name") == name:
            text = next(
                (c.get("text", "") for c in row.get("components") or []
                 if c.get("type") == "BODY"),
                "",
            )
            return TemplateStatus(
                status=(row.get("status") or "pending").lower(),
                rejection_reason=row.get("rejected_reason") or None,
                body=text,
            )
    return None


async def edit_template(
    *, template_id: str, token: str, body_text: str, sample_variables: dict[str, str],
) -> str:
    """Replace an existing template's body, keeping its name. Returns the status.

    The alternative — delete and recreate — is not one: Meta blocks reusing a
    deleted name for 30 days, so it would take every connected salon off the
    air for a month. An edit puts the template back to PENDING and Meta
    re-reviews it; the previously approved version keeps sending meanwhile.

    Only the body is sent: category is not editable this way (a UTILITY →
    MARKETING move doubles the cost of our highest-volume messages and gets a
    human, not a loop), and the name is the identity.
    """
    ordered = [sample_variables[str(i)] for i in range(1, len(sample_variables) + 1)]
    body = await _request(
        "POST", template_id, token=token,
        json_body={"components": [{
            "type": "BODY",
            "text": body_text,
            **({"example": {"body_text": [ordered]}} if ordered else {}),
        }]},
    )
    return (body.get("status") or "PENDING").lower()


async def delete_template(*, waba_id: str, name: str, token: str) -> None:
    """Remove a template from one WABA, by name.

    Deletes **every language version** of the name — Meta's by-name delete is
    not language-scoped, which is fine here because the catalogue is one
    language per key.

    Not idempotent-friendly at Meta: deleting a name that isn't there is an
    error, not a no-op. Callers that fan this out across many WABAs must treat
    "already gone" as success or a partial retry can never finish.

    **Meta blocks reusing a deleted template's name for 30 days.** That makes
    this a kill switch for a template that must stop going out, not an editing
    workflow — to change copy, add `promo_v2` and retire `promo_v1` once the
    new one is approved everywhere.
    """
    await _request(
        "DELETE", f"{waba_id}/message_templates", token=token,
        params={"name": name},
    )


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


# -------------------------------------------------------------------- document

async def create_document_template(
    *, waba_id: str, token: str, name: str, language: str,
    category: str, body_text: str, example_url: str,
) -> tuple[str, str]:
    """Create a DOCUMENT-header template (the Smart Receipt shape).

    `example_url` is a publicly-hosted sample PDF for Meta's review — mandatory
    for a document header, and the reason this is a separate call from
    `create_template` (which only ever emits a BODY). Returns (id, status).
    """
    body = await _request(
        "POST", f"{waba_id}/message_templates", token=token,
        json_body={
            "name": name,
            "language": language,
            "category": category,
            "components": [
                {
                    "type": "HEADER",
                    "format": "DOCUMENT",
                    "example": {"header_handle": [example_url]},
                },
                {"type": "BODY", "text": body_text},
            ],
        },
    )
    return body.get("id", ""), (body.get("status") or "PENDING").lower()


async def upload_media(
    *, phone_number_id: str, token: str, filename: str, content: bytes,
) -> str:
    """Upload a document to Meta, returning its media id for a document send.

    Multipart, not JSON — so it does not go through `_request`.
    """
    async with AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{GRAPH}/{phone_number_id}/media",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (filename, content, "application/pdf")},
            data={"messaging_product": "whatsapp", "type": "application/pdf"},
        )
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code >= 400:
            err = body.get("error") or {}
            raise MetaError(err.get("code"), err.get("message") or "media_upload_failed")
        return body.get("id", "")


async def send_document_template(
    *, phone_number_id: str, token: str, to: str,
    name: str, language: str, media_id: str, filename: str,
) -> str:
    """Send a DOCUMENT-header template: the uploaded PDF is the header document.

    Distinct from `send_template` (body variables) because the document is the
    whole payload — a receipt has no body variables to fill.
    """
    template = {
        "name": name,
        "language": {"code": language},
        "components": [{
            "type": "header",
            "parameters": [{
                "type": "document",
                "document": {"id": media_id, "filename": filename},
            }],
        }],
    }
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
