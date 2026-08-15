# API Reference

Contract documentation for `booking_engine/api/routes/*.py` — method, auth, and shape. Verified against the actual route handlers and `Depends()` chains, not inferred from naming.

> **Maintenance rule:** an endpoint added/removed/changed updates the matching file here in the same change. See [../README](../README.md#maintenance-rule).

---

## Response envelope

Most routes return `{"data": ...}` on success (see `voice.py::_wrap`, or a Pydantic `response_model` on the plain REST routes) and `{"error": "<code>", "message": "<human-readable>"}` on failure. A slot conflict on booking returns HTTP 409 with `{"error": "slot_taken", ...}`.

## Auth schemes — four distinct ones, don't mix them up

| Scheme | Header | Who | Routes |
|---|---|---|---|
| Control-plane bearer | `Authorization: Bearer <CONTROL_PLANE_SECRET>` | the `webapp` Control Plane | [Voice Control Plane](voice-control-plane.md), and [SMS](sms.md)'s `POST /sms/send` |
| Tool bearer | `Authorization: Bearer <OPENAI_TOOL_SECRET>` | OpenAI Realtime (tool calls + session events) | [Voice Tools](voice-tools.md), and the `/mcp` mount |
| Signature-verified webhook | `X-Twilio-Signature`, validated against `TWILIO_AUTH_TOKEN` | Twilio | [Telephony Webhooks](telephony-webhooks.md) and [SMS](sms.md)'s two webhooks (one shared verifier, `services/twilio_signature.py` — no-op if `TWILIO_AUTH_TOKEN` unset) |
| **None** | — | anyone who can reach the route | [Business API](business.md) (`shops`/`customers`/`services`/`availability`/`appointments`); `voice_openai.py`'s `/voice/openai/incoming` also has no *enforced* auth today — see [Telephony Webhooks](telephony-webhooks.md) |

Auth dependencies live in `booking_engine/api/deps.py` (`require_control_plane_token`, `require_tool_token`).

## Grouped pages

- **[Business API](business.md)** — plain CRUD REST: shops, staff, services, customers, availability, appointments.
- **[Telephony Webhooks](telephony-webhooks.md)** — the two inbound-call entrypoints (Twilio TwiML, OpenAI SIP accept).
- **[Voice Tools](voice-tools.md)** — the 12 OpenAI-callable tools, mounted via `/mcp` and dispatched in-process.
- **[Voice Control Plane](voice-control-plane.md)** — config, balance, heartbeat, telephony provisioning, calls/analytics — everything the webapp calls.
- **[SMS](sms.md)** — marketing SMS send plus the Twilio SMS webhooks (STOP handling, delivery status). Phase 1 of a larger messaging design; see [Architecture](../architecture.md#sms-marketing-send-phase-1-of-messaging).
