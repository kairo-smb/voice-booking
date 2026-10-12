# Operations

Deploy, migrations, CI, environment variables, and live-call testing.

> **Maintenance rule:** a change to CI, a migration workflow, a deploy step, or a required env var updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Branching & environments

Two Fly.io apps from the same `booking_engine/Dockerfile.fly`: production (`fly.toml`, app `kairo-booking-engine`, `min_machines_running = 1`) and QA (`fly.qa.toml`, app `kairo-booking-engine-qa`, `min_machines_running = 1`). Deploys automatically via GitHub Actions on push to `main` (production, `deploy-fly-prod.yml`) or `QA` (`deploy-qa.yml`) — both run tests and migration checks against a throwaway Neon branch first (see [Providers → Neon](providers.md#neon-postgresql)), then hand the real migration off to the `webapp` repo (below) before deploying.

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

`CONTROL_PLANE_SECRET` and `VOICE_AGENT_TOOL_SECRET` are Fly app secrets, not GitHub Actions secrets — `flyctl deploy` doesn't inject them:
```bash
fly secrets set CONTROL_PLANE_SECRET='...' VOICE_AGENT_TOOL_SECRET='...' --app kairo-booking-engine
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

## Testing WhatsApp automations end-to-end

`scripts/seed_automation_test.sql` and `scripts/cleanup_automation_test.sql` seed and remove one run of the automation rules on the QA demo shop, so a single `POST /api/v1/messaging/tick` fires a real template send. **They can be run from any machine**, not just the QA one: a local `psql` connecting to the QA Neon branch writes the same database the QA app reads, so the tick sees the seeded rows.

1. **Prerequisites on the QA demo shop** — the sender must be `online` and both templates `approved`. Attaching the sender is machine-bound: `scripts/seed_test_sender.py` asserts `SENTRY_ENVIRONMENT == 'qa'`, so run it on the QA machine. The seed script prints the sender row and both template rows, so the gates are visible before the tick runs.
2. **Seed:**
   ```bash
   psql "$QA_DATABASE_URL" -v confirm_qa=yes -v recipient='+39…' \
     -f scripts/seed_automation_test.sql
   ```
   Enables both rules and creates a due reminder (`scheduled`, start +2h) and a due feedback (`completed`, end −24h30m). `-v shop='…'` overrides the demo shop.
3. **Trigger:** `POST https://kairo-booking-engine-qa.fly.dev/api/v1/messaging/tick` with the control-plane bearer, or let the QA hourly scheduler fire it, then wait ≤60s for the drain — the automations stage enqueues and `whatsapp_sends` delivers on the next pass.
4. **Verify:** `whatsapp.outbound_messages WHERE campaign_key LIKE 'automation:%'` reaches `sent`/`delivered`/`read`, and both messages arrive.
5. **Re-run the tick → nothing new.** `whatsapp.automation_sends` dedupes on the same appointment.
6. **Clean up:**
   ```bash
   psql "$QA_DATABASE_URL" -v confirm_qa=yes -f scripts/cleanup_automation_test.sql
   ```
   Removes the seeded rows and disables the rules.

**Both scripts refuse production, twice:** an in-transaction guard checks psql's automatic `:HOST` variable against the production branch fragment (`ep-weathered-term-agsfwl6w`) and exits non-zero, and both require `-v confirm_qa=yes` (a missing or wrong value, or a missing `recipient`, refuses too). They run **committed**, not rolled back — that is the point: the tick stage must write real `outbound_messages` and `automation_sends` rows to prove the send path is wired. Only the scripts' own iterative validation used rolled-back transactions.

**Scope warning:** `run_automations` processes the **whole shop**, not just the seeded rows. Enabling a rule makes the next tick message *every* due appointment of that shop — the seed prints an `other_due_reminder`/`other_due_feedback` visibility query so the blast radius is known before triggering (the QA demo shop currently has two other due reminders). `min_no_shows = 0` in the seed means the reminder's no-show filter is inert, so the seeded run reaches everyone due.

**Automations debit no credits.** The salon's card is billed by Meta directly, unlike voice and SMS — see [Billing](api/whatsapp.md#billing).

## Running tests locally

```bash
pytest tests/ --ignore=tests/live_db -v          # no DB needed
DATABASE_URL=postgresql://... pytest tests/live_db/ -v   # real/ephemeral Neon branch
```

**Install from `booking_engine/requirements.txt`, not the root dev file, or "green locally" answers a different question than CI.** CI resolves the dependencies afresh on every run, so an unpinned requirement is a version nobody chose. That is not hypothetical: `fastapi>=0.115.0` let CI resolve 0.141.1 while a local venv held 0.124.4, and 0.141 changed how `include_router` builds the route table — the suite failed in CI on a test that passed locally. `fastapi` is now pinned to 0.141.1 (what CI and `kairo-booking-engine-qa` were both already running). To reproduce CI exactly rather than approximately:

```bash
python -m venv /tmp/ci && /tmp/ci/bin/pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio respx
/tmp/ci/bin/python -m pytest tests/voice_gateway/ tests/booking_engine/ -q
```

The remaining gap is deliberate: CI also runs `tests/live_db/` against an ephemeral Neon branch, which a local run skips unless `TEST_DATABASE_URL` is set. A green local run therefore says nothing about those.

## Scheduled jobs (in-process scheduler)

There is no external cron. `services/scheduler.py` runs inside every machine, started from the `asgi.py` lifespan; the cadences are fleet-wide and set per app in the Fly config (`0`/unset = job off):

| Job | Env | QA (`fly.qa.toml`) | Prod (`fly.toml`) |
|---|---|---|---|
| WhatsApp queue drain (`send_due`) | `WHATSAPP_SEND_LOOP_SECONDS` | 0 (off) | 0 (off) |
| Messaging tick (`run_tick`: bundles, health, release sweep, WA onboarding sweep, automations, nudges, retention) | `MESSAGING_TICK_SECONDS` | 3600 | 3600 |
| Forwarding heartbeat (push per silent shop, no dedupe — hence daily) | `FORWARDING_HEARTBEAT_SECONDS` | 86400 | 86400 |

- **Production is configured but not deployed** (voice-booking is not on prod yet): `fly.toml` carries the same three cadences as QA — **and** `min_machines_running = 1`, so the first prod deploy starts them. Nothing has ever run the tick on prod, so that first run does all pending work at once (provisioning, release sweep, template propagation, automations, queued sends): check the backlog before deploying.
- **The drain is off on purpose (2026-10-07).** Every slot opens a transaction (advisory lock + `requeue_stuck` UPDATE) even on an empty queue, so a 60s drain kept the Neon compute awake around the clock and 600s woke it every 10 minutes. The hourly tick already calls `send_due`, so queued sends (campaigns, automation reminders) go out within the hour instead. Anything that must leave immediately sends inline and skips the queue, like receipts and the single win-back. Don't turn it back on without weighing Neon compute time.
- **Needs a machine up.** Both apps keep `min_machines_running = 1` — at 0 Fly stops the machine and the jobs stop with it.
- **Safe at N machines.** Each job runs under a Postgres advisory lock, so one machine works and the rest skip that round. Jobs are aligned to the wall clock (the hourly one fires at :00), so every machine tries in the same instant, not N times per interval. `send_due` has **one lock for every caller** (drain job, tick, the inline win-back send): two concurrent drains would each read the daily cap and the per-customer cooldown before the other wrote, and each pace at full rate against Meta's app-level limit.
- **The lock is transaction-level on purpose.** `DATABASE_URL` goes through Neon's pgbouncer (transaction mode); a session-level `pg_try_advisory_lock` there excluded nothing when tested. `pg_try_advisory_xact_lock` inside an open transaction pins one server connection and dies with it. That transaction sets `idle_in_transaction_session_timeout = 0` locally: it idles for the whole job, and Neon's 5-minute default would otherwise kill the lock mid-drain. A QA restore-from-prod drops every connection — the running job fails, is logged, and the next slot retries.
- **Deploys:** a stopped machine rolls back its lock transaction; rows it left in `sending` are recovered by `requeue_stuck` on the next drain.
- `POST /api/v1/messaging/tick` still exists for manual runs and takes the same lock (`{"skipped": "busy"}` if the scheduler is mid-run).
