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
| `POST` | `/api/v1/sms/webhook/inbound` | `sms.py` | `X-Twilio-Signature` | STOP keyword handling |
| `POST` | `/api/v1/sms/webhook/status` | `sms.py` | `X-Twilio-Signature` | delivery status + the real Twilio price |

## `POST /api/v1/sms/send`

Body: `{shop_id, customer_id, body}` (`body` 1–1600 chars; blank rejected with 422).

On success: `{"data": {"message_id", "segments", "credits"}}`.

On refusal: **409**, not 400 — the request was well-formed, the current
state refuses it — with `{"detail": "<reason>"}`. Reasons:
`no_sender_number`, `customer_not_found`, `no_phone`, `no_consent`,
`opted_out`, `insufficient_credits`, `provider_error`.

The opt-out footer (`" Rispondi STOP per non ricevere piu'."`) is appended
server-side to every body before sanitizing/encoding and before sending —
never left to the caller or the LLM that wrote the draft. Credits are
computed off the resulting segment count and checked against the shop's
balance before Twilio is called; the debit itself only happens after
Twilio accepts the send (see `CLAUDE.md` §2026-08-12).

## `POST /api/v1/sms/webhook/inbound`

Twilio form fields `From`/`To`/`Body`. Always returns 200 + empty TwiML,
even for a non-STOP reply — a Twilio retry storm on an ordinary customer
reply helps nobody. A recognised STOP keyword (see
[Providers → SMS](../providers.md#sms-sending-twilio-messaging-api)) writes
both `sms.opt_outs` and withdraws
`business_app_core.customers.marketing_consent`.

## `POST /api/v1/sms/webhook/status`

Twilio form fields `MessageSid`/`MessageStatus`/`ErrorCode`/`Price`. Maps
Twilio's `delivered`/`sent`/`failed`/`undelivered` onto
`sms.outbound_messages.status` and writes back the real `price_usd`. The
credit charge recorded at send time is an estimate off Twilio's list price
and is **not** revised by this webhook — any drift between the estimate and
the real price is absorbed by Kairo, not re-billed to the shop.
