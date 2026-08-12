# Providers

Every external service this repo talks to: purpose, auth, and the hard rules that keep the integration safe. Verified against the actual client code.

> **Maintenance rule:** adding/removing/changing a provider integration updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Twilio

**Purpose:** inbound phone numbers and call routing. `clients/twilio_numbers.py` searches/purchases EU mobile numbers at onboarding time only; `api/routes/voice_twiml.py` handles the per-call dynamic TwiML webhook that routes an inbound call to the right shop and dials it into OpenAI.

**Key files:** `booking_engine/clients/twilio_numbers.py`, `booking_engine/api/routes/voice_twiml.py`, `booking_engine/api/routes/voice_telephony.py` (provisioning endpoints), `booking_engine/services/realtime_session.py::build_sip_uri` (how the shop id is attached to the outbound SIP dial).

**Env vars:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_DEFAULT_COUNTRY` (`EE`), `TWILIO_BUNDLE_SID`, `TWILIO_ADDRESS_SID`.

**Why Estonia, not Italy:** Estonia Mobile numbers are ~$3/mo vs. Italy Mobile's $30/mo (the only type Twilio sells there), with identical KYC friction (documents anywhere in the world, reused across every provisioned number via one Kairo-entity regulatory Bundle). Full country-by-country comparison in `CLAUDE.md` §2026-07-16.

**Gotcha:** `twilio_signature_valid()` (`booking_engine/services/twilio_signature.py`, extracted 2026-08-12 from `voice_twiml.py` so the TwiML webhook and the SMS webhooks below share one implementation) validates `X-Twilio-Signature` against `TWILIO_AUTH_TOKEN` — this is a real no-op (always accepts) if `TWILIO_AUTH_TOKEN` is unset, which is the case until the Twilio account is funded and configured.

### SMS sending (Twilio Messaging API)

**Purpose:** marketing SMS sends. Added 2026-08-12, Phase 1 of a larger
messaging design (`docs/messaging-design.md`; see
[Architecture → SMS marketing send](architecture.md#sms-marketing-send-phase-1-of-messaging)
and `CLAUDE.md` §2026-08-12). The shop's own Twilio DID — the same number
that answers voice calls — is also the SMS sender; no second number, no
shared Kairo sender.

**Key files:** `booking_engine/services/messaging/{sms_send,sms_inbound,gsm7,send_credits}.py`, `booking_engine/db/sms_queries.py`, `booking_engine/api/routes/sms.py`, `booking_engine/services/twilio_signature.py`.

**Env vars:** none new — reuses `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`. `sms_send.py` calls `twilio.rest.Client.messages.create()` directly; it does not go through `clients/twilio_numbers.py`, which is provisioning-only (search/purchase numbers).

**Gotcha — Twilio's automatic STOP handling doesn't cover this number.** Twilio auto-registers STOP/START keyword handling for US/Canada long codes only; the Estonian DID (2026-07-16 decision) gets none of it. `services/messaging/sms_inbound.py` reimplements STOP parsing entirely in application code — a whole-message match (never a substring match) against an IT/EN keyword list, so a reply like "non fermatevi, stop mai!" is not misread as an opt-out.

**Gotcha — segment/encoding is a billing surface, not cosmetic.** GSM-7 fits 160 chars/segment; the moment a body contains one character GSM-7 can't represent (most emoji, curly quotes, em dashes, uppercase accented vowels other than É), the whole message drops to UCS-2 at 70 chars/segment — silently tripling the bill on a stray LLM-written curly quote. `gsm7.py`'s `sanitize()` transliterates that typographic noise back into GSM-7 losslessly; genuinely non-GSM-7 content (emoji) is priced honestly, never silently stripped. Segment counting is duplicated in the webapp (`src/lib/messaging/sms-preview.ts`, a pre-click cost preview) rather than shared — this repo's count is authoritative at send time.

**Billing: 2× Twilio cost via AI credits, a dedicated converter.** `send_credits.py` — deliberately not the webapp's `rawToUserCredits()` (10× LLM margin, floors at 1 credit). Full reasoning: `CLAUDE.md` §2026-08-12.

## OpenAI Realtime

**Purpose:** speech-to-text, LLM reasoning, text-to-speech, and voice activity detection for the live call — via **native SIP** (production path) or an ephemeral browser/WebRTC session (local testing only).

**Key files:** `booking_engine/clients/openai_realtime.py` (`accept_sip_call`, `create_ephemeral_session`), `booking_engine/api/routes/voice_openai.py` (`realtime.call.incoming` webhook), `booking_engine/services/call_supervisor.py`, `booking_engine/mcp_server.py` (hosted MCP tool mount).

**Env vars:** `OPENAI_SIP_PROJECT_ID`, `OPENAI_API_KEY`, `OPENAI_REALTIME_MODEL` (`gpt-realtime` — not `gpt-4o-realtime-preview`), `OPENAI_WEBHOOK_SECRET`, `OPENAI_TOOL_SECRET`, `ENABLE_CALL_SUPERVISOR`, `CALL_SUPERVISOR_VERBOSE_LOGGING`.

**Hard-won gotcha #1 — hosted MCP does not auto-continue.** After a tool call, the model's response ends (`response.done` fires *before* the tool even returns); the tool executes, `response.output_item.done` delivers the result, and then nothing — OpenAI does not open a new response to voice it. This directly contradicts the Responses-API "hosted MCP auto-continues" assumption. Full event-trace evidence and the fix (a server-side control WebSocket sending `response.create`) in `CLAUDE.md` §2026-07-21 (two entries: "Realtime + hosted MCP..." and "SIP call supervisor...").

**Hard-won gotcha #2 — `server_url` needs a trailing slash.** `app.mount("/mcp", ...)` makes Starlette 307-redirect bare `/mcp` → `/mcp/`, and OpenAI's Realtime MCP client does **not** follow that redirect for the tool-call POST body — it silently never calls the tool. Always point `server_url` at `/mcp/`. Root-caused via `fly logs`; full story in `CLAUDE.md` §2026-07-21 "MCP server_url must carry a trailing slash".

**Gotcha #3 — webhook signature is opt-in.** `voice_openai.py`'s `realtime.call.incoming` handler only verifies a signature when `OPENAI_WEBHOOK_SECRET` is set (see the `ponytail:` comment at the top of that file) — currently unwired, so the endpoint accepts unsigned requests.

## Neon PostgreSQL

**Purpose:** the shared database — `business_app_core` (owned by the `webapp` Control Plane) plus `voice_agent` (owned by this repo). See [Database](database.md).

**Key files:** `booking_engine/db/connection.py` (asyncpg pool).

**Env vars:** `DATABASE_URL` (pooler endpoint, port 5432, transaction mode).

**CI usage:** every DB-touching GitHub Actions workflow (`ci.yml`, `deploy-qa.yml`, `deploy-fly-prod.yml`) provisions a throwaway, copy-on-write Neon branch off production, migrates + tests against it, then deletes it — never touches the real QA/production branch until that passes. Full rationale (a real seed-data bug this caught) in `CLAUDE.md` §2026-07-18.

**Migration ownership:** the ephemeral branch above is the only Neon branch this repo migrates itself. Applying migrations to the real QA/production branches belongs to the `webapp` repo, which orders all three schemas together — see [Operations → Migrations](operations.md#migrations) and `CLAUDE.md` §2026-07-24 "CI: migration ownership moved to the webapp repo".

## Push notifications (stub, not wired)

**Purpose:** intended to alert merchants (low balance, new callback memo, etc.) on their devices.

**Key files:** `booking_engine/clients/push_notifications.py`.

**Current state:** `send_push()` only logs the event — "Plan C wires this to the webapp's existing notification infrastructure" is a comment, not yet built. Don't assume any push notification actually reaches a merchant device today.
