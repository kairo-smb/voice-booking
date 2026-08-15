# Operations

Deploy, migrations, CI, environment variables, and live-call testing.

> **Maintenance rule:** a change to CI, a migration workflow, a deploy step, or a required env var updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Branching & environments

Two Fly.io apps from the same `booking_engine/Dockerfile.fly`: production (`fly.toml`, app `kairo-booking-engine`, `min_machines_running = 0`) and QA (`fly.qa.toml`, app `kairo-booking-engine-qa`, `min_machines_running = 1`). Deploys automatically via GitHub Actions on push to `main` (production, `deploy-fly-prod.yml`) or `QA` (`deploy-qa.yml`) — both run tests and migration checks against a throwaway Neon branch first (see [Providers → Neon](providers.md#neon-postgresql)), then hand the real migration off to the `webapp` repo (below) before deploying.

Manual deploy:
```bash
fly auth login
flyctl deploy --config fly.toml      # production
flyctl deploy --config fly.qa.toml   # QA
```

## Migrations

```bash
DATABASE_URL="$DATABASE_URL" ./scripts/migrate.sh
```
Applies every file in `booking_engine/db/sql/` in order, **except** `01_schema.sql`/`02_seed_data.sql` (a local-only bootstrap pair — the script skips them explicitly). See [Database](database.md) for what each migration adds.

**This repo does not migrate the shared QA/production branches itself.** `scripts/migrate.sh` is run here only against a *local* DB or the per-run ephemeral Neon branch. For real QA/prod, the `migrate-via-webapp` job in `deploy-qa.yml`/`deploy-fly-prod.yml` dispatches `kairo-smb/webapp`'s `migrate-qa.yml`/`migrate-prod.yml` and waits for it (20 min timeout) — the `webapp` repo is the parent that owns applying **all** schemas to the shared DB in order (`business_app_core` → `voice_agent` → `market_intel`), so this service can never deploy ahead of its schema. Refreshing the QA branch from production is likewise `webapp`'s job now, not this repo's.

## Secrets

`CONTROL_PLANE_SECRET` and `OPENAI_TOOL_SECRET` are Fly app secrets, not GitHub Actions secrets — `flyctl deploy` doesn't inject them:
```bash
fly secrets set CONTROL_PLANE_SECRET='...' OPENAI_TOOL_SECRET='...' --app kairo-booking-engine
```
`WEBAPP_MIGRATE_DISPATCH_TOKEN` is the opposite case — a **GitHub Actions** repo secret only (a token with `actions:write` on `kairo-smb/webapp`), never a Fly secret. Without it the `migrate-via-webapp` job fails and neither environment deploys.

Full env var list: `booking_engine/config.py`'s `Settings` class is the exhaustive source; the auth-relevant subset is restated per-provider in [Providers](providers.md).

## Post-deploy smoke test

Pick any active shop UUID from the DB:
```bash
URL='https://kairo-booking-engine.fly.dev'
SECRET='<CONTROL_PLANE_SECRET>'
SHOP_ID='<existing shop UUID>'
H="Authorization: Bearer $SECRET"

curl -s -o /dev/null -w '%{http_code} (expect 401)\n' "$URL/api/v1/voice/config/$SHOP_ID"
curl -s -H "$H" "$URL/api/v1/voice/config/$SHOP_ID" | jq
curl -s -H "$H" -H 'Content-Type: application/json' \
  -X PATCH "$URL/api/v1/voice/config/$SHOP_ID" \
  -d '{"greeting_after_disclosure":"Smoke test"}' | jq
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/calls" | jq
curl -s -H "$H" "$URL/api/v1/shops/$SHOP_ID/voice/analytics" | jq
```
Pass: step 1 → `401`; steps 2-3 → `{"data": {...}}` with the expected config; steps 4-5 → `{"data": [...] | {...}}`, empty/zeroed for a fresh shop.

## Testing a real call without a phone

`scripts/voice_test_server.py`'s browser/WebRTC harness (`./scripts/run_webrtc_harness.sh`) is convenient but a different transport than production — real calls arrive over SIP. You don't need a funded Twilio number to test the real SIP path: OpenAI's SIP gateway accepts a call from *any* SIP client dialed straight at the project's SIP URI, firing the exact same `realtime.call.incoming` webhook a Twilio-forwarded call would.

1. Install a SIP softphone that supports TLS (e.g. [Linphone](https://www.linphone.org/en/), or `pjsua` from `pjproject`).
2. Get a shop UUID from the QA Neon branch, and the OpenAI SIP project id (same value as the `OPENAI_SIP_PROJECT_ID` Fly secret on `kairo-booking-engine-qa`).
3. Get the dial target and header:
   ```bash
   set -a; source .env; set +a
   python scripts/print_sip_test_uri.py <shop_id>
   ```
   This prints a bare dial URI (`sip:{project}@sip.api.openai.com;transport=tls`) and a separate custom header (`X-Shop-Id: {shop_id}`) — **a raw softphone dial has no Twilio in the path to attach that header for you.** Without it, the call reaches OpenAI but has no shop to route to and gets rejected before ringing. Add it via your softphone's custom-header support if it has one (`pjsua --help | grep -i header`, or a GUI client's custom-headers field).
4. Watch `fly logs -a kairo-booking-engine-qa`.

To also exercise the call-supervisor fix (greeting + post-tool speech) and see full debug output:
```bash
fly secrets set ENABLE_CALL_SUPERVISOR=true CALL_SUPERVISOR_VERBOSE_LOGGING=true --app kairo-booking-engine-qa
# test call, then: fly logs -a kairo-booking-engine-qa — confirm a "supervisor.greeted" line
fly secrets unset ENABLE_CALL_SUPERVISOR CALL_SUPERVISOR_VERBOSE_LOGGING --app kairo-booking-engine-qa
```
`CALL_SUPERVISOR_VERBOSE_LOGGING` also turns on caller-speech transcription (normally off) — keep it off outside a deliberate debug session, since it puts full conversation content into `fly logs`.

## Running tests locally

```bash
pytest tests/ --ignore=tests/live_db -v          # no DB needed
DATABASE_URL=postgresql://... pytest tests/live_db/ -v   # real/ephemeral Neon branch
```
