# QA Neon Branch + Fly QA App + CI/CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Tasks 3, 6, and 7 involve real infrastructure changes (creating a Neon
> branch, setting GitHub secrets, triggering a real deploy) and require
> human-in-the-loop confirmation at the point of action — do not dispatch
> these to a subagent that can't pause for user input. Tasks 1, 2, 4, 5 are
> plain file-creation/edit tasks safe for normal subagent dispatch.**

**Goal:** Stand up a persistent Neon `QA` branch, a `kairo-booking-engine-qa`
Fly app, and CI/CD pipelines that refresh/test/migrate/deploy QA on push to
`QA` and mirror the same pipeline for production on push to `main`.

**Architecture:** Two new GitHub Actions workflows (`deploy-qa.yml`,
`deploy-fly-prod.yml`), a new shell migration runner
(`scripts/migrate.sh`), and a new Fly config (`fly.qa.toml`) alongside the
existing `fly.toml`. Existing `ci.yml` and `deploy.yml` (Lambda) are
untouched.

**Tech Stack:** GitHub Actions, Neon CLI (`neonctl`), Fly (`flyctl`),
`psql`, bash.

**Prerequisite (done):** `QA` branch merged with `feat/voice-forwarding-overflow`
so `fly.toml` correctly points at `booking_engine/Dockerfile.fly` (commit
`14c0837`, pushed to `origin/QA`).

---

### Task 1: Migration runner script

**Files:**
- Create: `scripts/migrate.sh`
- Test: manual verification via a throwaway local Postgres container (no
  pytest — this is a shell script, verified by actually running it)

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
set -euo pipefail

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL must be set" >&2
  exit 1
fi

for f in booking_engine/db/sql/*.sql; do
  echo "Applying $f..."
  psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$f"
done

echo "All migrations applied."
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/migrate.sh
```

- [ ] **Step 3: Verify it fails cleanly with no DATABASE_URL**

```bash
unset DATABASE_URL; ./scripts/migrate.sh
```
Expected: prints `DATABASE_URL must be set` to stderr, exits non-zero.

- [ ] **Step 4: Verify it applies cleanly against a throwaway local Postgres**

```bash
docker run -d --name migrate-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=migrate_test -p 5433:5432 postgres:17
sleep 3
DATABASE_URL="postgresql://postgres:postgres@localhost:5433/migrate_test" ./scripts/migrate.sh
```
Expected: each of the 9 files in `booking_engine/db/sql/` applies without
error, ending with `All migrations applied.`

- [ ] **Step 5: Verify re-running is a safe no-op (idempotency)**

```bash
DATABASE_URL="postgresql://postgres:postgres@localhost:5433/migrate_test" ./scripts/migrate.sh
docker rm -f migrate-test
```
Expected: second run also completes with `All migrations applied.` and no
errors — proves the existing idempotent-migration convention holds.

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate.sh
git commit -m "feat(ci): add scripts/migrate.sh to apply booking_engine SQL migrations"
```

---

### Task 2: Fly QA app + config

**Files:**
- Create: `fly.qa.toml`

- [ ] **Step 1: Write the config (copy of `fly.toml` with a different app name)**

```toml
app = 'kairo-booking-engine-qa'
primary_region = 'fra'

[build]
  dockerfile = 'booking_engine/Dockerfile.fly'

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = 'stop'
  auto_start_machines = true
  min_machines_running = 0

  [http_service.concurrency]
    type = 'requests'
    hard_limit = 250
    soft_limit = 200

[[vm]]
  memory = '512mb'
  cpu_kind = 'shared'
  cpus = 1
```

- [ ] **Step 2: Create the Fly app itself (one-time; requires your Fly login — do this yourself, not via subagent)**

```bash
flyctl apps create kairo-booking-engine-qa
```
Expected: `New app created: kairo-booking-engine-qa`

- [ ] **Step 3: Validate the config against the newly created app**

```bash
flyctl config validate -c fly.qa.toml -a kairo-booking-engine-qa
```
Expected: `Configuration is valid`

- [ ] **Step 4: Commit**

```bash
git add fly.qa.toml
git commit -m "feat(ci): add fly.qa.toml for the QA Fly app"
```

---

### Task 3: Neon QA branch (human-in-the-loop — do not dispatch to a subagent)

This step creates real, shared infrastructure. Confirm with the user
immediately before invoking, per the Neon MCP tool's own safety notice.

- [ ] **Step 1: Confirm with the user, then create the branch**

Use the Neon MCP `create_branch` tool (or equivalent `neonctl` command) with:
- `projectId`: `falling-bread-89568725`
- `branchName`: `QA`
- No `parentId` (branches from the project's default branch, `production`)

Equivalent CLI form, if not using the MCP tool:
```bash
neonctl branches create --project-id falling-bread-89568725 --name QA
```

- [ ] **Step 2: Fetch the QA branch's connection string**

```bash
neonctl connection-string QA --project-id falling-bread-89568725 \
  --role-name <role from existing DATABASE_URL> \
  --database-name <db name from existing DATABASE_URL>
```
(Use the Neon MCP `get_connection_string` tool instead if available — pass
`branchId: "QA"`.) Do not print the resulting connection string in the
transcript — pipe it directly into the `gh secret set` command in Task 6.

- [ ] **Step 3: Verify the branch exists and has the full schema**

```bash
neonctl branches list --project-id falling-bread-89568725
```
Expected: a branch named `QA` appears, parented from `production`, with the
same tables as production (copy-on-write — no separate verification query
needed).

---

### Task 4: `deploy-qa.yml` workflow

**Files:**
- Create: `.github/workflows/deploy-qa.yml`
- Test: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-qa.yml'))"` (syntax check — no YAML linter installed in this repo)

- [ ] **Step 1: Write the workflow**

```yaml
name: Deploy QA

# QA release pipeline. Pushes to QA refresh the Neon QA branch from
# production, run tests, apply migrations to QA, then deploy the QA Fly app.
# Production is never touched by this workflow.

on:
  push:
    branches: [QA]
  workflow_dispatch:

concurrency:
  group: booking-engine-qa-release
  cancel-in-progress: false

jobs:
  refresh-qa-db:
    name: Refresh Neon QA from production
    runs-on: ubuntu-latest
    timeout-minutes: 10
    env:
      NEON_API_KEY: ${{ secrets.NEON_API_KEY }}
      NEON_PROJECT_ID: ${{ vars.NEON_PROJECT_ID }}
      NEON_PROD_BRANCH: ${{ vars.NEON_PROD_BRANCH || 'production' }}
      NEON_QA_BRANCH: ${{ vars.NEON_QA_BRANCH || 'QA' }}
    steps:
      - name: Guard - Neon settings must be set
        run: |
          missing=0
          for name in NEON_API_KEY NEON_PROJECT_ID NEON_PROD_BRANCH NEON_QA_BRANCH; do
            if [ -z "${!name}" ]; then
              echo "::error::${name} is not set."
              missing=1
            fi
          done
          exit "${missing}"

      - name: Install Neon CLI
        run: |
          curl -fsSL https://github.com/neondatabase/neon-pkgs/releases/latest/download/neonctl-linux-x64 -o /tmp/neonctl
          chmod +x /tmp/neonctl
          sudo mv /tmp/neonctl /usr/local/bin/neonctl
          neonctl --version

      - name: Restore QA branch from production
        run: |
          neonctl branches restore "${NEON_QA_BRANCH}" "${NEON_PROD_BRANCH}" \
            --project-id "${NEON_PROJECT_ID}"

  test:
    name: Unit + live_db tests
    needs: refresh-qa-db
    runs-on: ubuntu-latest
    timeout-minutes: 12
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: kairo_test
        ports: ['5432:5432']
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      DATABASE_URL: postgresql://postgres:postgres@localhost:5432/kairo_test
      TEST_DATABASE_URL: postgresql://postgres:postgres@localhost:5432/kairo_test
      LOCAL_DB: postgresql://postgres:postgres@localhost:5432/kairo_test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip

      - run: pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio

      - name: Run unit tests
        run: pytest tests/voice_gateway/ tests/booking_engine/ -q

      - name: Install PostgreSQL 17 client
        run: |
          sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg
          sudo apt-get update -qq
          sudo apt-get install -y -qq postgresql-client-17

      - name: Clone Neon schema into local Postgres
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          pg_dump --schema-only --no-owner --no-privileges \
            "${{ secrets.CI_SCHEMA_SOURCE_URL }}" > /tmp/schema.sql
          psql -v ON_ERROR_STOP=1 "$LOCAL_DB" -f /tmp/schema.sql

      - name: Load seed data into business_app_core
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          PGOPTIONS="-c search_path=business_app_core,public" \
            psql -v ON_ERROR_STOP=1 "$LOCAL_DB" -f booking_engine/db/sql/02_seed_data.sql

      - name: Run live_db tests
        run: pytest tests/live_db/ -q

  migrate-qa:
    name: Migrate QA DB
    needs: test
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - name: Guard - QA database URL must be set
        run: |
          if [ -z "${DATABASE_URL}" ]; then
            echo "::error::QA_DATABASE_URL secret is not set."
            exit 1
          fi
        env:
          DATABASE_URL: ${{ secrets.QA_DATABASE_URL }}
      - name: Install PostgreSQL 17 client
        run: |
          sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg
          sudo apt-get update -qq
          sudo apt-get install -y -qq postgresql-client-17
      - name: Apply migrations to QA
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          chmod +x scripts/migrate.sh
          ./scripts/migrate.sh
        env:
          DATABASE_URL: ${{ secrets.QA_DATABASE_URL }}

  deploy-qa:
    name: Deploy QA
    needs: migrate-qa
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Setup flyctl
        uses: superfly/flyctl-actions/setup-flyctl@master
      - name: Deploy to QA Fly app
        env:
          FLY_API_TOKEN: ${{ secrets.QA_FLY_API_TOKEN }}
        run: flyctl deploy --remote-only --config fly.qa.toml
      - name: Smoke check QA health
        run: |
          for attempt in {1..30}; do
            if curl --fail --silent --show-error https://kairo-booking-engine-qa.fly.dev/health; then
              exit 0
            fi
            sleep 5
          done
          echo "QA health check did not pass within 150 seconds" >&2
          exit 1
```

- [ ] **Step 2: Verify YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-qa.yml'))" && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy-qa.yml
git commit -m "feat(ci): add deploy-qa.yml — refresh, test, migrate, deploy QA Fly app"
```

---

### Task 5: `deploy-fly-prod.yml` workflow

**Files:**
- Create: `.github/workflows/deploy-fly-prod.yml`
- Test: same YAML syntax check as Task 4

- [ ] **Step 1: Write the workflow**

```yaml
name: Deploy Fly Production

# Production Fly release pipeline for the voice-webhook/MCP surface
# (kairo-booking-engine). Separate from deploy.yml (Lambda REST API) — same
# codebase, different compute target. Order enforced: tests, then migrations
# to production, then deploy — a regression or failing migration stops the
# release before prod is touched.

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: booking-engine-prod-release
  cancel-in-progress: false

jobs:
  test:
    name: Unit + live_db tests
    runs-on: ubuntu-latest
    timeout-minutes: 12
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: kairo_test
        ports: ['5432:5432']
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      DATABASE_URL: postgresql://postgres:postgres@localhost:5432/kairo_test
      TEST_DATABASE_URL: postgresql://postgres:postgres@localhost:5432/kairo_test
      LOCAL_DB: postgresql://postgres:postgres@localhost:5432/kairo_test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip

      - run: pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio

      - name: Run unit tests
        run: pytest tests/voice_gateway/ tests/booking_engine/ -q

      - name: Install PostgreSQL 17 client
        run: |
          sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg
          sudo apt-get update -qq
          sudo apt-get install -y -qq postgresql-client-17

      - name: Clone Neon schema into local Postgres
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          pg_dump --schema-only --no-owner --no-privileges \
            "${{ secrets.CI_SCHEMA_SOURCE_URL }}" > /tmp/schema.sql
          psql -v ON_ERROR_STOP=1 "$LOCAL_DB" -f /tmp/schema.sql

      - name: Load seed data into business_app_core
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          PGOPTIONS="-c search_path=business_app_core,public" \
            psql -v ON_ERROR_STOP=1 "$LOCAL_DB" -f booking_engine/db/sql/02_seed_data.sql

      - name: Run live_db tests
        run: pytest tests/live_db/ -q

  migrate-prod:
    name: Migrate production DB
    needs: test
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - name: Guard - production database URL must be set
        run: |
          if [ -z "${DATABASE_URL}" ]; then
            echo "::error::DATABASE_URL secret is not set."
            exit 1
          fi
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
      - name: Install PostgreSQL 17 client
        run: |
          sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg
          sudo apt-get update -qq
          sudo apt-get install -y -qq postgresql-client-17
      - name: Apply migrations to production
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          chmod +x scripts/migrate.sh
          ./scripts/migrate.sh
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}

  deploy-prod:
    name: Deploy production Fly app
    needs: migrate-prod
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Setup flyctl
        uses: superfly/flyctl-actions/setup-flyctl@master
      - name: Deploy to production Fly app
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
        run: flyctl deploy --remote-only --config fly.toml
      - name: Smoke check production health
        run: |
          for attempt in {1..30}; do
            if curl --fail --silent --show-error https://kairo-booking-engine.fly.dev/health; then
              exit 0
            fi
            sleep 5
          done
          echo "Production health check did not pass within 150 seconds" >&2
          exit 1
```

- [ ] **Step 2: Verify YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-fly-prod.yml'))" && echo OK
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy-fly-prod.yml
git commit -m "feat(ci): add deploy-fly-prod.yml — test, migrate, deploy production Fly app"
```

---

### Task 6: GitHub secrets/vars (human-in-the-loop — run yourself, do not paste secret values into chat)

- [ ] **Step 1: Set repo variables**

```bash
gh variable set NEON_PROJECT_ID --body "falling-bread-89568725"
gh variable set NEON_PROD_BRANCH --body "production"
gh variable set NEON_QA_BRANCH --body "QA"
```

- [ ] **Step 2: Set repo secrets (run these yourself with real values — do not send the values through chat)**

```bash
gh secret set NEON_API_KEY               # paste the same value used in webapp/marketing-engine
gh secret set QA_DATABASE_URL             # the QA branch connection string from Task 3, Step 2
gh secret set FLY_API_TOKEN               # flyctl tokens create deploy -x 999999h (or reuse an existing org token)
gh secret set QA_FLY_API_TOKEN            # can be the same value as FLY_API_TOKEN
```

- [ ] **Step 3: While here, also close the pre-existing Twilio secrets gap (blocks the existing Lambda deploy.yml today, unrelated to this plan but already flagged as outstanding)**

```bash
gh secret set TWILIO_ACCOUNT_SID
gh secret set TWILIO_AUTH_TOKEN
gh secret set TWILIO_BUNDLE_SID
gh secret set TWILIO_ADDRESS_SID
gh secret delete TELNYX_API_KEY
gh secret delete TELNYX_PUBLIC_KEY
```

- [ ] **Step 4: Verify**

```bash
gh variable list
gh secret list
```
Expected: `NEON_PROJECT_ID`, `NEON_PROD_BRANCH`, `NEON_QA_BRANCH` in variables;
`NEON_API_KEY`, `QA_DATABASE_URL`, `FLY_API_TOKEN`, `QA_FLY_API_TOKEN`,
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_BUNDLE_SID`,
`TWILIO_ADDRESS_SID` in secrets; `TELNYX_API_KEY`/`TELNYX_PUBLIC_KEY` gone.

---

### Task 7: End-to-end verification (human-in-the-loop)

- [ ] **Step 1: Push Tasks 1, 2, 4, 5's commits to `origin/QA`**

```bash
git push origin QA
```

- [ ] **Step 2: Watch the `deploy-qa.yml` run**

```bash
gh run watch --exit-status
```
Expected: `refresh-qa-db` → `test` → `migrate-qa` → `deploy-qa` all succeed in
order; final smoke check reaches `https://kairo-booking-engine-qa.fly.dev/health`.

- [ ] **Step 3: Confirm the QA MCP endpoint is reachable**

```bash
curl -s https://kairo-booking-engine-qa.fly.dev/health
```
Expected: `200 OK` health response, same shape as production's `/health`.

- [ ] **Step 4: Merge QA into main (only once satisfied) to trigger `deploy-fly-prod.yml`, and watch it**

```bash
git checkout main && git pull && git merge QA --no-edit && git push origin main
gh run watch --exit-status
```
Expected: `test` → `migrate-prod` → `deploy-prod` succeed; smoke check reaches
`https://kairo-booking-engine.fly.dev/health`.

---

## Self-Review Notes

- **Spec coverage:** all 7 sections of the spec (Neon QA branch, Fly QA app,
  migration runner, both new workflows, secrets checklist, out-of-scope
  voice-harness note) map to Tasks 1–7 above; the voice-harness item is
  deliberately not a task here, matching the spec's "out of scope."
- **Placeholder scan:** no TBD/TODO; every step has literal commands or file
  contents.
- **Type/name consistency:** `scripts/migrate.sh` name matches across Tasks
  1, 4, 5; `fly.qa.toml` / `kairo-booking-engine-qa` naming matches across
  Tasks 2, 4, 7; secret names (`QA_DATABASE_URL`, `FLY_API_TOKEN`,
  `QA_FLY_API_TOKEN`, `NEON_*`) match exactly between Task 6 and the
  workflows in Tasks 4–5.
