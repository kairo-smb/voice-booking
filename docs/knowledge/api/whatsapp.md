# WhatsApp API

`booking_engine/api/routes/whatsapp.py`, mounted at `/api/v1`. Onboards a salon onto WhatsApp with its own sender, and queues personalised marketing that drips out across the day.

**Read [Providers → WhatsApp](../providers.md#whatsapp-meta-tech-provider--twilio) first** if you're new to this: the constraints (approved templates only, one WABA per Twilio account, per-recipient marketing caps) explain why these endpoints exist in this shape.

## Auth

| Routes | Scheme |
|---|---|
| `/whatsapp/onboarding/*`, `/whatsapp/status/*`, `/whatsapp/templates/*`, `/whatsapp/campaigns*` | Control-plane bearer (`CONTROL_PLANE_SECRET`) — the webapp is the only caller |
| `/whatsapp/webhook/status`, `/whatsapp/webhook/inbound` | `X-Twilio-Signature`, validated against **the salon's subaccount auth token** (`whatsapp.senders.subaccount_auth_token`), not `TWILIO_AUTH_TOKEN` |
| `/whatsapp/webhook/otp` | `X-Twilio-Signature`, validated against `TWILIO_AUTH_TOKEN` — the number is in the parent account |

The subaccount distinction is not cosmetic. Twilio signs a webhook with the auth token of the account that *owns the resource*; validating a subaccount's WhatsApp traffic against the parent token rejects every genuine request.

---

## Onboarding

Three calls, because Meta's Embedded Signup is a browser popup with no server-side equivalent — a WABA can only be created by the salon itself.

### `POST /whatsapp/onboarding/start`

```json
{ "shop_id": "…", "display_name": "Salone Bellezza", "source": "kairo", "phone_number": null }
```

`source`:
- `"kairo"` (default) — reuse `voice_agent.shop_telephony.kairo_number`, the number the salon already answers calls on. No second number, no second $3/mo, no regulatory bundle inside the subaccount. Requires the shop to already have a provisioned number ([Number Provisioning](number-provisioning.md)).
- `"salon"` — the salon's own number, in `phone_number`. It must not already be active on WhatsApp or WhatsApp Business App; they have to delete that account first.

Creates the salon's Twilio subaccount (idempotent — an existing one is reused) and returns the Embedded Signup config the webapp needs:

```json
{"data": {"ok": true, "status": "pending_signup", "phone_number": "+372…",
          "signup": {"app_id": "…", "config_id": "…",
                     "phone_number": "+372…", "business_name": "Salone Bellezza"}}}
```

Errors (HTTP 409): `no_kairo_number`, `invalid_phone`, `invalid_source`.

### `POST /whatsapp/onboarding/waba`

```json
{ "shop_id": "…", "waba_id": "1234567890" }
```

Called once the salon closes Meta's popup. Registers the sender via Twilio's Senders API (`account_type: ISVSubAccount`) and returns `status: "verifying"`.

For `source="kairo"` this also temporarily binds the number's inbound-SMS webhook to `/whatsapp/webhook/otp`, so Meta's ownership code is caught automatically. It is unbound again the moment the sender goes online.

Errors: `not_started`.

### `POST /whatsapp/onboarding/verify`

```json
{ "shop_id": "…", "code": "123456" }
```

`source="salon"` only — the salon types the code Meta sent them. Returns `status: "online"` on success, at which point the template catalogue is created and submitted automatically.

Errors: `not_registered`.

### `GET /whatsapp/status/{shop_id}`

Everything the webapp needs to render the waiting/ready state:

```json
{"data": {"status": "online", "source": "kairo", "phone_number": "+372…",
          "display_name": "Salone Bellezza", "quality_rating": "HIGH",
          "messaging_limit": "1K Customers/24hr", "daily_cap": 50,
          "offline_reason": null, "sent_today": 12,
          "templates": [{"template_key": "promo_v1", "status": "approved"}]}}
```

`status` is `not_started | pending_signup | verifying | online | offline | failed`. A salon is only able to send when `status == "online"` **and** at least one template is `approved`.

### `POST /whatsapp/templates/ensure/{shop_id}`

Re-runs template creation for anything missing. Use after a rejection or after the catalogue (`services/messaging/whatsapp_templates.py`) gains an entry. Already-created templates are skipped — Meta blocks reusing a deleted template's name for 30 days.

---

## Campaigns

### `POST /whatsapp/campaigns`

```json
{
  "shop_id": "…",
  "campaign_key": "agosto-winback",
  "template_key": "promo_v1",
  "recipients": [
    {"customer_id": "…", "variables": {"1": "Giulia", "2": "Salone Bellezza",
                                        "3": "questa settimana taglio e piega a 35€."}}
  ]
}
```

There is **no `body` field, and there cannot be one.** A business-initiated WhatsApp marketing message is an approved template plus variable values — the caller supplies the values, the template supplies everything else. The webapp's LLM copy generator writes `{{3}}`, not the message.

Returns immediately with the schedule; nothing is sent inline:

```json
{"data": {"ok": true, "queued": 48, "suppressed": 2, "already_sent": 0,
          "first_at": "2026-08-21T09:00:00+02:00",
          "last_at": "2026-08-21T19:47:00+02:00"}}
```

- `suppressed` — a row was written with `suppressed_reason` (`no_consent`, `no_phone`, `customer_not_found`). Refusals are recorded, never silent.
- `already_sent` — this `campaign_key` already reached that customer; the unique index made the retry a no-op.

Errors (409): `sender_not_online`, `unknown_template`, `template_rejected` / `template_pending` / `template_paused`, `over_daily_cap`.

### `DELETE /whatsapp/campaigns/{shop_id}/{campaign_key}`

Cancels whatever hasn't gone out yet (`queued`/`sending` → `cancelled`). Already-sent rows are untouched history.

---

## Webhooks

### `POST /whatsapp/webhook/status`

Twilio delivery status. Maps `sent|delivered|read|failed|undelivered`; `read` is a signal SMS never had.

**It is also the opt-out sink.** `ErrorCode` `63033` or `63050` means the recipient used Meta's native "Stop promotions" button, so the handler writes `marketing_consent = false` back to `business_app_core.customers`. This is the self-service opt-out the SMS channel gave up on 2026-08-15 — see [Decisions](../decisions.md).

### `POST /whatsapp/webhook/inbound`

A customer replied. Logged only. The reply opens Meta's 24-hour session window (inside which free-form messages *are* allowed); nothing in this repo uses that yet.

### `POST /whatsapp/webhook/otp`

Meta's ownership-verification SMS, landing on a Kairo-owned number. Matches a 6-digit run and submits it, and only for a shop actually mid-verification. Bound only while verifying, unbound on success.

This is **not** a reintroduction of STOP handling (removed 2026-08-15): nothing here parses message content beyond a numeric code, and the hook does not survive onboarding.

---

## The hourly tick

`POST /messaging/tick` ([Number Provisioning](number-provisioning.md)) gained two WhatsApp stages, each independently wrapped so one failure can't suppress the others:

- `whatsapp` — polls Twilio/Meta for sender verification and template approval verdicts. Neither has a webhook we receive today, so polling is the only way a salon's status ever stops saying "in attesa".
- `whatsapp_sends` — claims what is due and sends it. Counts: `sent`, `suppressed`, `failed`, `deferred` (over daily cap, retried in an hour), `requeued` (claimed but never sent, recovered from a crashed tick).
