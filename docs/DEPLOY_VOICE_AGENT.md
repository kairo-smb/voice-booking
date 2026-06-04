# Voice Agent Control Plane — Deploy Guide

Manual deploy steps for the Booking Engine after the `voice_agent` schema + endpoints landed on `main`.

## Prerequisites

- AWS CLI configured with credentials for the target account (`aws sts get-caller-identity` should work).
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

## 3. Deploy the Booking Engine to AWS Lambda

The existing `scripts/deploy-booking.sh` builds and pushes the container image. The script reads two env vars and forwards them to Lambda as runtime env vars.

```bash
AWS_REGION=eu-central-1 \
  DATABASE_URL='postgresql://neondb_owner:...@...neon.tech/neondb?sslmode=require&channel_binding=require' \
  CONTROL_PLANE_SECRET='paste-your-32-byte-hex-here' \
  ./scripts/deploy-booking.sh
```

The first run creates ECR repo + IAM role + Lambda function + Function URL. Subsequent runs only update the container image.

> If `deploy-booking.sh` does not currently forward `CONTROL_PLANE_SECRET` to the Lambda environment, edit it before running — search for `Environment` / `aws lambda update-function-configuration` and add the variable to the `Variables` map. The variable name MUST be `CONTROL_PLANE_SECRET` (matches `booking_engine/config.py`).

## 4. Record the Function URL

The script prints the deployed Function URL on success — something like:
```
https://abcde12345.lambda-url.eu-central-1.on.aws/
```

You will use this as `VOICE_AGENT_API_URL` in the webapp.

## 5. Post-deploy smoke test

Pick any active shop UUID from your DB. Replace placeholders below.

```bash
URL='https://abcde12345.lambda-url.eu-central-1.on.aws'
SECRET='paste-your-32-byte-hex-here'
SHOP_ID='<existing shop UUID>'
H="Authorization: Bearer $SECRET"

# 1. Auth required
curl -s -o /dev/null -w '%{http_code} (expect 401)\n' "$URL/api/v1/shops/$SHOP_ID/voice/config"

# 2. Read config (expect 200 with defaults: voice=alloy, language=it)
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/config" | jq

# 3. Update config
curl -s -H "$H" -H 'Content-Type: application/json' \
  -X PATCH "$URL/api/v1/shops/$SHOP_ID/voice/config" \
  -d '{"welcome_message":"Smoke test"}' | jq

# 4. List calls (expect empty)
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/calls" | jq

# 5. Analytics (expect zeros)
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/analytics" | jq
```

Pass criteria:
- Step 1 returns `401`.
- Steps 2 and 3 return `{ "data": { ... } }` with the expected voice/language values.
- Steps 4 and 5 return `{ "data": [...] | {...} }` with empty/zeroed shapes.

## 6. Hand off to the webapp deploy

Set the two env vars in the webapp's deployment environment (AWS Amplify or local `.env`):

```
VOICE_AGENT_API_URL=https://abcde12345.lambda-url.eu-central-1.on.aws
VOICE_AGENT_SECRET=paste-your-32-byte-hex-here
```

Plan 2 (`docs/superpowers/plans/2026-05-30-inbox-voice-agent-ui.md` in the webapp repo) consumes these.

## Rollback

The migration is purely additive — no existing tables or columns were altered. Rolling back the Lambda is enough:
```bash
aws lambda update-function-code \
  --function-name booking-engine \
  --image-uri <previous ECR image URI>
```

If you also want to drop the new schema (destructive — deletes call history):
```sql
DROP SCHEMA voice_agent CASCADE;
ALTER TABLE business_app_core.shops DROP COLUMN voice, DROP COLUMN language;
```

---

## Plan A — Platform deploy notes (2026-06-03)

### New env vars on Lambda

- `TWILIO_ACCOUNT_SID` — Twilio account SID
- `TWILIO_AUTH_TOKEN` — Twilio auth token
- `OPENAI_SIP_PROJECT_ID` — OpenAI project ID for SIP routing
- `PUBLIC_BASE_URL` — public URL of the API Gateway in front of Lambda (used for TwiML voice_url)
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

### Scheduled job

Add an EventBridge rule that triggers the forwarding heartbeat nightly at 09:00 Europe/Rome. The heartbeat queries `voice_agent.shop_telephony` for Path-2 shops with no inbound calls in 5 days and emits `forwarding_might_be_off` push events.
