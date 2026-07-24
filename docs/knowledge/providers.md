# Providers

Every external service this repo talks to: purpose, auth, and the hard rules that keep the integration safe. Verified against the actual client code.

> **Maintenance rule:** adding/removing/changing a provider integration updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Twilio

**Purpose:** inbound phone numbers and call routing. `clients/twilio_numbers.py` searches/purchases EU mobile numbers at onboarding time only; `api/routes/voice_twiml.py` handles the per-call dynamic TwiML webhook that routes an inbound call to the right shop and dials it into OpenAI.

**Key files:** `booking_engine/clients/twilio_numbers.py`, `booking_engine/api/routes/voice_twiml.py`, `booking_engine/api/routes/voice_telephony.py` (provisioning endpoints), `booking_engine/services/realtime_session.py::build_sip_uri` (how the shop id is attached to the outbound SIP dial).

**Env vars:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_DEFAULT_COUNTRY` (`EE`), `TWILIO_BUNDLE_SID`, `TWILIO_ADDRESS_SID`.

**Why Estonia, not Italy:** Estonia Mobile numbers are ~$3/mo vs. Italy Mobile's $30/mo (the only type Twilio sells there), with identical KYC friction (documents anywhere in the world, reused across every provisioned number via one Kairo-entity regulatory Bundle). Full country-by-country comparison in `CLAUDE.md` §2026-07-16.

**Gotcha:** `_twilio_signature_valid()` in `voice_twiml.py` validates `X-Twilio-Signature` against `TWILIO_AUTH_TOKEN` — this is a real no-op (always accepts) if `TWILIO_AUTH_TOKEN` is unset, which is the case until the Twilio account is funded and configured.

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

## Push notifications (stub, not wired)

**Purpose:** intended to alert merchants (low balance, new callback memo, etc.) on their devices.

**Key files:** `booking_engine/clients/push_notifications.py`.

**Current state:** `send_push()` only logs the event — "Plan C wires this to the webapp's existing notification infrastructure" is a comment, not yet built. Don't assume any push notification actually reaches a merchant device today.
