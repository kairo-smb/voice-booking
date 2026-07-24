# Telephony Webhooks

The two entrypoints that start a call — see [Architecture → Call flow](../architecture.md#call-flow) for how they chain together.

> **Maintenance rule:** a change to either webhook's routing/auth updates this file in the same change. See [../README](../README.md#maintenance-rule).

---

## `POST /api/v1/voice/twiml/incoming`

**File:** `voice_twiml.py`. Twilio calls this when a call reaches one of this repo's provisioned numbers. Looks up the shop by dialed number, returns TwiML that `<Dial><Sip>`s into OpenAI with the shop id attached as a custom SIP header.

**Auth:** `X-Twilio-Signature`, validated via `RequestValidator(TWILIO_AUTH_TOKEN)`. **No-op (always valid) if `TWILIO_AUTH_TOKEN` is unset.**

## `POST /voice/openai/incoming`

**File:** `voice_openai.py`. Note: **not** under `/api/v1` — mounted at the top level in `app.py`. OpenAI fires `realtime.call.incoming` here. Reads the shop id back out of the SIP headers (or `SIP_TEST_FALLBACK_SHOP_ID` on QA for a raw softphone test with no Twilio in the path), resolves the caller, assembles the session prompt, and calls `accept_sip_call()`.

**Auth:** signature verified only when `OPENAI_WEBHOOK_SECRET` is set (see the `ponytail:` comment at the top of the file) — currently unwired, so this endpoint accepts unsigned requests. Known gap, not yet closed.
