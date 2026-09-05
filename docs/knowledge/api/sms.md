# SMS

Marketing SMS send + Twilio SMS webhooks. Phase 1 of a larger messaging
design — see [Architecture → SMS marketing send](../architecture.md#sms-marketing-send-phase-1-of-messaging)
for the end-to-end flow and [Database → `sms` schema](../database.md#sms-schema--authoritative-here)
for the tables.

> **Maintenance rule:** an endpoint added/removed/changed here updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

| Method | Path | File | Auth | Purpose |
|---|---|---|---|---|
| `POST` | `/api/v1/sms/send` | `sms.py` | Control-plane bearer | synchronous one-off marketing send; called by the webapp's `send-sms` route |
| `POST` | `/api/v1/sms/webhook/status` | `sms.py` | `X-Twilio-Signature` | delivery status + the real Twilio price |

## `POST /api/v1/sms/send`

Body: `{shop_id, customer_id, body}` (`body` 1–1600 chars; blank rejected with 422).

On success: `{"data": {"message_id", "segments", "credits"}}`.

On refusal: **409**, not 400 — the request was well-formed, the current
state refuses it — with `{"detail": "<reason>"}`. Reasons:
`no_sender_number`, `customer_not_found`, `no_phone`, `no_consent`,
`insufficient_credits`, `provider_error`.

**No in-message opt-out.** STOP handling (footer + inbound webhook) was
removed — see `CLAUDE.md`'s STOP-removal entry. The body sent is the
sanitised text alone; suppression is `business_app_core.customers.
marketing_consent` alone, cleared in-store by staff, not by a customer
reply. Credits are computed off the resulting segment count and checked
against the shop's balance before Twilio is called; the debit itself only
happens after Twilio accepts the send (see `CLAUDE.md` §2026-08-12). The
send now also passes Twilio a `status_callback` (built from
`public_base_url` + this file's own `/webhook/status` path below) so
delivery status/price actually come back — previously omitted, so
`sms.outbound_messages` rows stayed `status='sent'`/`price_usd=NULL` forever.

## `POST /api/v1/sms/webhook/status`

Twilio form fields `MessageSid`/`MessageStatus`/`ErrorCode`/`Price`. Maps
Twilio's `delivered`/`sent`/`failed`/`undelivered` onto
`sms.outbound_messages.status` and writes back the real `price_usd`. The
credit charge recorded at send time is an estimate off Twilio's list price
and is **not** revised by this webhook — any drift between the estimate and
the real price is absorbed by Kairo, not re-billed to the shop.
