# Voice Agent Control Plane — Deploy Guide

Manual deploy steps for the Booking Engine after the `voice_agent` schema + endpoints landed on `main`.

## Prerequisites

- `flyctl` installed and authenticated (`fly auth login`).
- `DATABASE_URL` for the Neon production database (pooler endpoint).
- A `CONTROL_PLANE_SECRET` value — generate one if you don't already have one stored.

## 1. Schema is already applied

The `voice_agent` schema and the new `shops.voice` / `shops.language` columns were applied to the live Neon DB during development (see commit `6f58870`). The migration file (`booking_engine/db/sql/03_voice_agent_schema.sql`) is idempotent — re-running it on `main` is safe but unnecessary.

If you ever bootstrap a fresh Neon branch, run:
```bash
psql "$DATABASE_URL" -f booking_engine/db/sql/03_voice_agent_schema.sql
```
or the catch-all:
```bash
DATABASE_URL="$DATABASE_URL" ./scripts/setup_neon.sh
```

## 2. Generate the control-plane secret

If you haven't already, generate a 32-byte hex secret:
```bash
openssl rand -hex 32
```
Save it somewhere durable (1Password / AWS Secrets Manager / etc.). You will paste it into the deploy command below AND into the webapp's environment as `VOICE_AGENT_SECRET`.

## 3. Deploy the Booking Engine to Fly.io

Booking Engine deploys automatically via GitHub Actions on push to `main`
(`.github/workflows/deploy-fly-prod.yml`) — it runs tests, validates
migrations on a throwaway Neon branch, applies them to production, then
`flyctl deploy --config fly.toml`. `DATABASE_URL` is already configured as
a GitHub repo secret for the migration step. `CONTROL_PLANE_SECRET` is a
Fly app secret, not a GitHub one — `flyctl deploy` doesn't inject it, so
it must be set once directly on the Fly app if not already present:

```bash
fly secrets set CONTROL_PLANE_SECRET='paste-your-32-byte-hex-here' --app kairo-booking-engine
```

For a manual deploy:

```bash
fly auth login
flyctl deploy --config fly.toml
```

## 4. Record the deployed URL

The Fly app serves at its stable app URL — something like:
```
https://kairo-booking-engine.fly.dev
```

You will use this as `VOICE_AGENT_API_URL` in the webapp.

## 5. Post-deploy smoke test

Pick any active shop UUID from your DB. Replace placeholders below.

```bash
URL='https://kairo-booking-engine.fly.dev'
SECRET='paste-your-32-byte-hex-here'
SHOP_ID='<existing shop UUID>'
H="Authorization: Bearer $SECRET"

# 1. Auth required
curl -s -o /dev/null -w '%{http_code} (expect 401)\n' "$URL/api/v1/voice/config/$SHOP_ID"

# 2. Read config (expect 200 with shop_config row, voice_preset defaults to verse)
curl -s -H "$H" "$URL/api/v1/voice/config/$SHOP_ID" | jq

# 3. Update config
curl -s -H "$H" -H 'Content-Type: application/json' \
  -X PATCH "$URL/api/v1/voice/config/$SHOP_ID" \
  -d '{"greeting_after_disclosure":"Smoke test"}' | jq

# 4. List calls (expect empty)
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/calls" | jq

# 5. Analytics (expect zeros)
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/analytics" | jq
```

Pass criteria:
- Step 1 returns `401`.
- Steps 2 and 3 return `{ "data": { ... } }` with the expected shop_config values.
- Steps 4 and 5 return `{ "data": [...] | {...} }` with empty/zeroed shapes.

## 6. Hand off to the webapp deploy

Set the two env vars in the webapp's deployment environment (AWS Amplify or local `.env`):

```
VOICE_AGENT_API_URL=https://kairo-booking-engine.fly.dev
VOICE_AGENT_SECRET=paste-your-32-byte-hex-here
```

Plan 2 (`docs/superpowers/plans/2026-05-30-inbox-voice-agent-ui.md` in the webapp repo) consumes these.

## Rollback

The migration is purely additive — no existing tables or columns were altered. Rolling back the Fly app is enough:
```bash
flyctl releases list --app kairo-booking-engine
flyctl deploy --image <previous-image-ref> --app kairo-booking-engine
```

If you also want to drop the new schema (destructive — deletes call history):
```sql
DROP SCHEMA voice_agent CASCADE;
ALTER TABLE business_app_core.shops DROP COLUMN voice, DROP COLUMN language;
```

---

## Plan A — Platform deploy notes (2026-06-03)

### New env vars on Fly.io

- `TWILIO_ACCOUNT_SID` — Twilio account SID
- `TWILIO_AUTH_TOKEN` — Twilio auth token
- `TWILIO_DEFAULT_COUNTRY` — default `EE` (Estonia mobile numbers; see the 2026-07-16 Telnyx→Twilio migration spec)
- `TWILIO_BUNDLE_SID` — the one Kairo-entity regulatory Bundle, created and approved once, out-of-band, in the Twilio Console — reused across every provisioned DID; leave unset until it exists, `purchase_number` treats it as optional
- `TWILIO_ADDRESS_SID` — regulatory Address tied to the same Bundle, if Twilio requires one for the number type; also optional
- `OPENAI_SIP_PROJECT_ID` — OpenAI project ID for SIP routing
- `PUBLIC_BASE_URL` — public URL of the Fly app (used for TwiML voice_url)
- `VOICE_KAIRO_TOKENS_PER_SECOND` — default 18
- `VOICE_MIN_SESSION_RESERVE_TOKENS` — default 1500
- `VOICE_MAX_OVERAGE_TOKENS` — default 5000

### Migration

Run once against prod:
```bash
psql "$DATABASE_URL" -f booking_engine/db/sql/04_voice_agent_v2.sql
```

This migration is additive — no existing columns are modified. It adds:
- `source`, `phone_normalized`, `household_of`, `phone_shared_with` to `customers`
- `source`, `voice_call_id`, `confirmation_status` to `appointments`
- `voice_agent.shop_telephony` — Twilio number provisioning table
- `voice_agent.shop_config` expansions — fallback, auto-topup, enabled columns
- `voice_agent.callback_memos` — merchant callback reminders
- `voice_agent.auth_events` — identity verification audit trail
- `voice_agent.system_policy` — disclosure/consent text (seeded with it-IT)
- `voice_agent.calls` extensions — `shop_id`, `matched_customer_id`, `created_customer_id`, `created_booking_id`
- `ai_token_basket_events.voice_call_id` — links debit events to calls

### New API endpoints

All endpoints require `Authorization: Bearer <CONTROL_PLANE_SECRET>`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/voice/numbers/search?area_code=02` | Search Twilio available numbers |
| POST | `/api/v1/voice/numbers/provision` | Purchase and bind a Kairo number |
| GET | `/api/v1/voice/numbers/{shop_id}` | Get telephony config for a shop |
| POST | `/api/v1/voice/twiml/incoming` | Dynamic TwiML webhook (Twilio calls this) |
| GET | `/api/v1/voice/config/{shop_id}` | Read voice agent config |
| PATCH | `/api/v1/voice/config/{shop_id}` | Update voice agent config |
| GET | `/api/v1/voice/balance/{shop_id}` | Token balance + warning tier for webapp |
| POST | `/api/v1/voice/heartbeat/forwarding?threshold_days=5` | Path-2 silent-line heartbeat (EventBridge target) |

### Scheduled job

Add an EventBridge rule that POSTs to `/api/v1/voice/heartbeat/forwarding` nightly at 09:00 Europe/Rome (07:00 UTC). The endpoint queries `voice_agent.shop_telephony` for Path-2 shops with no inbound calls in 5 days and emits `forwarding_might_be_off` push events. Requires the `Authorization: Bearer <CONTROL_PLANE_SECRET>` header.

### Twilio webhook URL

The `voice_url` configured on each purchased number is `${PUBLIC_BASE_URL}/api/v1/voice/twiml/incoming`. Twilio request signatures are verified against `TWILIO_AUTH_TOKEN` — if the token is unset (e.g. local dev), validation is skipped.

---

## Plan B — Tools & identity deploy notes (2026-06-03)

### New env vars on Fly.io

- `OPENAI_TOOL_SECRET` — bearer secret for OpenAI tool/event webhooks

### OpenAI configuration

Configure these webhook URLs in the OpenAI Voice dashboard:

- POST `{public_base_url}/voice/events/session.started`
- POST `{public_base_url}/voice/events/session.turn`
- POST `{public_base_url}/voice/events/session.ended`

Tool endpoints under `{public_base_url}/voice/tools/*` with `Authorization: Bearer <OPENAI_TOOL_SECRET>`.

### No new migrations — uses tables from Plan A's migration 04

All tables (`voice_agent.calls`, `voice_agent.auth_events`, `voice_agent.callback_memos`, `customers`, `appointments`, etc.) were created by the Plan A migration `04_voice_agent_v2.sql`.

## Testing over real SIP, no Twilio required (2026-07-22)

`scripts/voice_test_server.py`'s browser/WebRTC harness is convenient but is a
different transport than production (real calls arrive over SIP). You don't
need a working Twilio number to test the real SIP path — OpenAI's SIP gateway
accepts a call from *any* SIP client dialed straight at the project's SIP URI,
which fires the exact same `realtime.call.incoming` webhook
(`booking_engine/api/routes/voice_openai.py`) a Twilio-forwarded call would.
This is the QA webhook already registered in the OpenAI dashboard, so it's
safe to test against freely (writes land on the QA Neon branch).

1. Install any SIP softphone that supports TLS, e.g. [Linphone](https://www.linphone.org/en/) (GUI) or `pjsua` (CLI, ships with `pjproject`).
2. Get the OpenAI SIP project ID from the OpenAI dashboard (Project → Settings → SIP) — it's the same value stored as the `OPENAI_SIP_PROJECT_ID` Fly secret on `kairo-booking-engine-qa`, not readable back out of Fly.
3. Get a shop UUID from the QA Neon branch (any row in `voice_agent.shop_config`, or your local `DEMO_SHOP_ID`).
4. Get the dial target and header:
   ```bash
   set -a; source .env; set +a
   python scripts/print_sip_test_uri.py <shop_id>
   ```
   This prints two things — **they are not the same mechanism**:
   - A bare dial URI: `sip:{OPENAI_SIP_PROJECT_ID}@sip.api.openai.com;transport=tls`
   - A custom header to send alongside it: `X-Shop-Id: {shop_id}`

   For a **real Twilio call**, `voice_twiml.py::_dial_sip` (via `realtime_session.py::build_sip_uri`) passes the shop id as `sip:{project}@host?X-Shop-Id={shop_id}` — Twilio's documented `<Dial><Sip>` convention for custom headers (query string *after* the host). Twilio itself translates that into a real `X-Shop-Id` SIP header on the INVITE it sends OpenAI, which `shop_id_from_sip_headers()` reads back out.

   **A raw softphone/pjsua dial gets none of that translation** — there's no Twilio in the path to do it. Dialing the bare URI alone will reach OpenAI and exercise the prompt/tools/model config, but **without a real `X-Shop-Id` header attached, the call has no shop to route to** and will get rejected before ringing. To attach it yourself:
   - Check if your pjsua build supports it: `pjsua --help | grep -i header`. If so, add it as a real header (e.g. `--add-header "X-Shop-Id: <uuid>"` — confirm exact flag syntax against your build, it's changed across PJSIP versions).
   - If unsupported, a GUI softphone with a "custom headers" field per-account (some do) is the alternative.

   (Historical note: this whole SIP-test-doc section originally had `X-Shop-Id` embedded *before* the `@` — `sip:{project};X-Shop-Id={shop_id}@host` — which is neither valid bare SIP URI syntax nor Twilio's actual convention. A live raw SIP test call caught it: OpenAI echoed back a `To` header built from it that pjsip couldn't even parse. Since Twilio has never been funded/tested live, this was a real, never-exercised bug in `_dial_sip` too, not just a doc error — fixed in `build_sip_uri` alongside this doc.)
5. Watch `fly logs -a kairo-booking-engine-qa` for the call.

**Note:** `ENABLE_CALL_SUPERVISOR` is currently off on QA (not in `fly secrets list`), so the agent will greet-and-go-mute-after-tools per the known gap (see CLAUDE.md, "Realtime + hosted MCP does NOT auto-speak tool results"). To also verify the supervisor fix (still pending its first live check per CLAUDE.md) and see full debug output — agent + caller transcripts, MCP tool arguments/output, every raw event — set both flags for the test session:
```bash
fly secrets set ENABLE_CALL_SUPERVISOR=true CALL_SUPERVISOR_VERBOSE_LOGGING=true --app kairo-booking-engine-qa
# test call, then: fly logs -a kairo-booking-engine-qa
# confirm a "supervisor.greeted" line, post-tool speech, and full event content in each log line
fly secrets unset ENABLE_CALL_SUPERVISOR CALL_SUPERVISOR_VERBOSE_LOGGING --app kairo-booking-engine-qa
```
`CALL_SUPERVISOR_VERBOSE_LOGGING` does two things: logs the full raw event (not just type/tool/latency), and turns on `audio.input.transcription` so the caller's speech is transcribed too (normally off — nothing in production reads it). Keep this off outside a deliberate debug session: it puts full conversation content into `fly logs`.
