# QA Neon Branch + Fly QA App + CI/CD — Design

**Status:** Approved. Implementation plan to follow.

## Motivation

We need to test the full voice-agent chain (Twilio webhook → OpenAI Realtime →
MCP tool calls → Neon writes) without mutating production booking data, and
without every test call requiring a manual `flyctl deploy`. Today:

- The `kairo-booking-engine` Fly app (voice webhook + MCP surface) is deployed
  by hand (`flyctl deploy` from a developer's machine).
- `booking_engine/db/sql/*.sql` migrations are applied by hand via `psql -f`
  against production — no automation, no QA equivalent.
- There is no isolated environment to point a test harness (e.g. the
  browser-based voice test harness discussed alongside this spec) at without
  risking writes to real shop data.

This spec adds a persistent Neon `QA` branch, a second Fly app
(`kairo-booking-engine-qa`) pointed at it, and CI/CD pipelines for both QA and
production, following the exact conventions already proven in the `webapp`
and `marketing-engine` repos (same Neon project, `falling-bread-89568725`).

## Architecture

```
push to QA  ──▶ deploy-qa.yml:
                  1. neonctl restore QA from production   (refresh QA data)
                  2. run tests (unit + live_db, same as ci.yml)
                  3. scripts/migrate.sh against QA_DATABASE_URL
                  4. flyctl deploy --config fly.qa.toml
                  5. smoke-check https://kairo-booking-engine-qa.fly.dev/health

push to main ──▶ deploy-fly-prod.yml:
                  1. run tests
                  2. scripts/migrate.sh against production DATABASE_URL
                  3. flyctl deploy --config fly.toml
                  4. smoke-check https://kairo-booking-engine.fly.dev/health

(existing, unchanged) deploy.yml on push to main:
                  Lambda REST API deploy — separate compute target, same
                  codebase, not touched by this work.

(existing, unchanged) ci.yml on PR into main/QA:
                  unit + live_db tests against a schema clone — not touched.
```

Neon: one project, two branches (`production`, `QA`). QA is refreshed from
production at the start of every QA deploy, so it always starts from a fresh
copy of real data before that run's migrations apply — no permanent
divergence, no manual reset needed.

Fly: two apps, same Docker image (`booking_engine/Dockerfile.fly`), same
Twilio/OpenAI credentials (per the approved design decision — there is no
Twilio/OpenAI "test mode" to isolate; isolation comes from the DB branch, not
separate telephony/AI accounts). Only `DATABASE_URL` (and `PUBLIC_BASE_URL`,
which must point at each app's own hostname) differ between the two apps'
secrets.

## Components

### `scripts/migrate.sh` (new)

A minimal shell script — not a framework — that applies
`booking_engine/db/sql/*.sql` in filename order against `$DATABASE_URL`:

```bash
#!/usr/bin/env bash
set -euo pipefail
for f in booking_engine/db/sql/*.sql; do
  echo "Applying $f..."
  psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$f"
done
```

No migration-tracking table. The existing SQL files are already written to be
idempotent (numbered, `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` style, per
the project's established migration convention) — re-running the full set on
every deploy is safe and matches how they've been applied manually so far.
Used identically by both the QA and prod pipelines; also usable by hand
(`DATABASE_URL=... ./scripts/migrate.sh`) as a replacement for today's
one-off `psql -f` commands.

### `fly.qa.toml` (new)

Copy of `fly.toml` with `app = 'kairo-booking-engine-qa'`. Same
`Dockerfile.fly`, same `[http_service]`/`[[vm]]` shape.

### `.github/workflows/deploy-qa.yml` (new)

Triggers on push to the `QA` git branch. Jobs, in order:

1. **refresh-qa-db** — `neonctl branches restore QA production --project-id "$NEON_PROJECT_ID"`. Guarded: fails fast if `NEON_API_KEY`/`NEON_PROJECT_ID`/`NEON_PROD_BRANCH`/`NEON_QA_BRANCH` aren't set.
2. **test** (`needs: refresh-qa-db`) — same steps as `ci.yml`'s `unit` and `db-tests` jobs (schema-clone into a local Postgres service container, run `pytest`). Kept independent of the QA Neon branch itself, matching `marketing-engine`'s pattern — fast, no risk to the QA branch from test runs.
3. **migrate-qa** (`needs: test`) — `scripts/migrate.sh` with `DATABASE_URL=${{ secrets.QA_DATABASE_URL }}`.
4. **deploy-qa** (`needs: migrate-qa`) — `flyctl deploy --config fly.qa.toml` using `QA_FLY_API_TOKEN`, then polls `https://kairo-booking-engine-qa.fly.dev/health` for up to 150s.

Concurrency group `booking-engine-qa-release`, `cancel-in-progress: false` (never let two QA releases race).

### `.github/workflows/deploy-fly-prod.yml` (new)

Triggers on push to `main`. Same job shape as `deploy-qa.yml` minus the
Neon-restore step (production is never restored from anything):

1. **test** — same as above.
2. **migrate-prod** (`needs: test`) — `scripts/migrate.sh` with `DATABASE_URL=${{ secrets.DATABASE_URL }}` (the existing production secret, already set).
3. **deploy-prod** (`needs: migrate-prod`) — `flyctl deploy --config fly.toml` using `FLY_API_TOKEN`, then smoke-checks `https://kairo-booking-engine.fly.dev/health`.

Concurrency group `booking-engine-prod-release`, `cancel-in-progress: false`.

The existing `.github/workflows/deploy.yml` (Lambda REST API) and `ci.yml`
(PR gate) are unchanged — this is a parallel deploy path for the Fly
voice-webhook/MCP surface specifically.

## Secrets/vars checklist (manual, out-of-band)

Confirmed via `gh secret list` / `gh variable list` on this repo — these are
**not yet set** and must be added before the new workflows can run:

| Name | Type | Value / source |
|---|---|---|
| `NEON_PROJECT_ID` | var | `falling-bread-89568725` (same project as webapp/marketing-engine) |
| `NEON_PROD_BRANCH` | var | `production` |
| `NEON_QA_BRANCH` | var | `QA` |
| `NEON_API_KEY` | secret | copy from webapp/marketing-engine's repo secrets (same Neon account) |
| `QA_DATABASE_URL` | secret | QA branch's pooler connection string (create the `QA` branch once manually or via `neonctl branches create`, then fetch its connection string) |
| `FLY_API_TOKEN` | secret | Fly API token for prod deploys |
| `QA_FLY_API_TOKEN` | secret | Fly API token for QA deploys (can be the same token as above if scoped org-wide) |

Also still outstanding from the earlier Telnyx→Twilio migration (blocking the
*existing* Lambda `deploy.yml` today, unrelated to this spec but worth fixing
alongside it): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_BUNDLE_SID`,
`TWILIO_ADDRESS_SID`. Leftover unused `TELNYX_API_KEY`/`TELNYX_PUBLIC_KEY`
secrets are dead (nothing references them) and can be deleted.

## Out of scope

- The browser-based voice test harness (WebRTC + MCP) discussed alongside
  this spec — it depends on this infrastructure existing
  (`kairo-booking-engine-qa.fly.dev/mcp`) and will be designed/planned
  separately once this lands.
- Any change to the existing Lambda REST API deploy path (`deploy.yml`) or
  PR-gate CI (`ci.yml`).
- A real migration-tracking system (versioned/rollback-capable migrations).
  Not needed while all migrations remain additive/idempotent by convention.
- Separate Twilio/OpenAI credentials for QA (explicitly decided against —
  same real credentials, isolation via the DB branch only).
