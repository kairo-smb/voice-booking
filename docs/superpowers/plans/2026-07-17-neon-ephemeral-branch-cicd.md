# Neon Ephemeral-Branch CI/CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace voice-booking's local-Postgres-container + fake-seed CI mechanism with real, throwaway Neon branches (copy-on-write from production) — matching `webapp`'s existing pattern — for PR checks, QA releases, and prod releases alike, and fix the seed-script bug that has blocked every QA/prod deploy since 2026-07-16.

**Architecture:** Every DB-touching workflow provisions a uniquely-named Neon branch off `production`, applies real migrations (`scripts/migrate.sh`) and fixture data (`02_seed_data.sql`, now schema-portable) to it, runs `tests/live_db/` against it, then deletes it. Only after that passes does a push-triggered workflow mutate the real QA branch or production. The now-superseded AWS Lambda deploy path is removed.

**Tech Stack:** GitHub Actions, Neon CLI (`neonctl`), `psql` (PostgreSQL 17 client), Python 3.12/pytest, flyctl.

**Spec:** `docs/superpowers/specs/2026-07-17-neon-ephemeral-branch-cicd-design.md`

---

### Task 1: Fix the `staff_schedules` seed-data bug

**Files:**
- Modify: `booking_engine/db/sql/02_seed_data.sql:71-75`

The real Neon `business_app_core.staff_schedules` table has only a plain
(non-unique) index on `(staff_id, day_of_week)` — never a matching unique
constraint — so `ON CONFLICT (staff_id, day_of_week) DO NOTHING` fails with
`ERROR: there is no unique or exclusion constraint matching the ON CONFLICT
specification` every time it runs against real Neon. `CLAUDE.md` forbids
changing the shared schema, so the fix rewrites the query to a
constraint-independent equivalent.

- [ ] **Step 1: Replace the ON CONFLICT clause**

Change `booking_engine/db/sql/02_seed_data.sql` lines 71-75 from:

```sql
INSERT INTO staff_schedules (id, staff_id, day_of_week, start_time, end_time)
SELECT gen_random_uuid(), s.id, d.day, '10:00', '18:00'
FROM staff s
CROSS JOIN (VALUES (0),(1),(2),(3),(4),(5)) AS d(day)
ON CONFLICT (staff_id, day_of_week) DO NOTHING;
```

to:

```sql
INSERT INTO staff_schedules (id, staff_id, day_of_week, start_time, end_time)
SELECT gen_random_uuid(), s.id, d.day, '10:00', '18:00'
FROM staff s
CROSS JOIN (VALUES (0),(1),(2),(3),(4),(5)) AS d(day)
WHERE NOT EXISTS (
  SELECT 1 FROM staff_schedules ss
  WHERE ss.staff_id = s.id AND ss.day_of_week = d.day
);
```

- [ ] **Step 2: Verify against the real schema on a throwaway Neon branch**

Use the Neon MCP tools (already available in this session) rather than
docker — this proves the fix against the actual production schema, not an
approximation of it:

1. `mcp__plugin_neon_neon__create_branch` with `projectId: falling-bread-89568725`, `parentId` set to the production branch, name e.g. `verify-seed-fix`.
2. `mcp__plugin_neon_neon__run_sql` against that branch: run the full
   `booking_engine/db/sql/02_seed_data.sql` contents (schema-qualify by
   prefixing statements with `SET search_path TO business_app_core, public;`
   first, or pass `databaseName`/schema context consistent with how CI
   will invoke it). Confirm it completes with no error.
3. Run the same SQL a second time against the same branch (proves the
   `WHERE NOT EXISTS` guard is idempotent — row counts for `staff_schedules`
   must not change between the two runs). Query:
   `SELECT count(*) FROM business_app_core.staff_schedules WHERE staff_id IN (SELECT id FROM business_app_core.staff WHERE shop_id = 'a0000000-0000-0000-0000-000000000001');`
   before and after the second run — counts must match.
4. `mcp__plugin_neon_neon__delete_branch` to clean up `verify-seed-fix`.

- [ ] **Step 3: Commit**

```bash
git add booking_engine/db/sql/02_seed_data.sql
git commit -m "fix(seed): make staff_schedules insert constraint-independent

ON CONFLICT (staff_id, day_of_week) targeted a unique constraint that
only exists in the local bootstrap schema, never in real Neon (which
has a plain non-unique index on the same columns). Rewritten as a
WHERE NOT EXISTS guard so it works identically against both schemas."
```

---

### Task 2: Rewrite `ci.yml` to use ephemeral Neon branches

**Files:**
- Modify: `.github/workflows/ci.yml` (full rewrite of the `db-tests` job; `unit` job unchanged)

- [ ] **Step 1: Replace the file contents**

```yaml
name: CI

# On PR into main/QA: fast unit tests gate every change, and a db-tests job
# provisions a throwaway Neon branch (copy-on-write from production),
# applies migrations + seed fixtures to it, and runs the live_db suite
# against it. This is the compliance check: voice-booking's queries run
# against the real, current shape of the shared business_app_core schema.
# Production is only ever read (copy-on-write); every write lands on the
# throwaway branch, which is destroyed afterwards, so prod is never mutated.

on:
  pull_request:
    branches: [main, QA]
  workflow_dispatch:

jobs:
  unit:
    name: Unit tests (no DB)
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
      - run: pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio
      - name: Run unit tests
        run: pytest tests/voice_gateway/ tests/booking_engine/ -q

  db-tests:
    name: live_db against ephemeral Neon branch
    runs-on: ubuntu-latest
    timeout-minutes: 12
    env:
      NEON_API_KEY: ${{ secrets.NEON_API_KEY }}
      NEON_PROJECT_ID: ${{ vars.NEON_PROJECT_ID }}
      NEON_PROD_BRANCH: ${{ vars.NEON_PROD_BRANCH || 'production' }}
      QA_DATABASE_URL: ${{ secrets.QA_DATABASE_URL }}
      CI_BRANCH: booking-ci-${{ github.run_id }}-${{ github.run_attempt }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip

      - name: Guard - required secrets/vars must be set
        run: |
          missing=0
          for name in NEON_API_KEY NEON_PROJECT_ID NEON_PROD_BRANCH QA_DATABASE_URL; do
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

      - name: Install PostgreSQL 17 client
        run: |
          sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg
          sudo apt-get update -qq
          sudo apt-get install -y -qq postgresql-client-17

      - name: Provision ephemeral Neon branch from production
        run: |
          set -euo pipefail
          neonctl branches create \
            --project-id "$NEON_PROJECT_ID" \
            --parent "$NEON_PROD_BRANCH" \
            --name "$CI_BRANCH" >/dev/null
          role=$(python3 -c "import os,urllib.parse as up; print(up.urlparse(os.environ['QA_DATABASE_URL']).username)")
          db=$(python3 -c "import os,urllib.parse as up; print(up.urlparse(os.environ['QA_DATABASE_URL']).path.lstrip('/').split('?')[0])")
          conn=$(neonctl connection-string "$CI_BRANCH" \
            --project-id "$NEON_PROJECT_ID" \
            --role-name "$role" \
            --database-name "$db")
          if [ -z "$conn" ]; then
            echo "::error::Empty connection string for branch $CI_BRANCH"
            exit 1
          fi
          echo "::add-mask::$conn"
          echo "CI_DB_URL=$conn" >> "$GITHUB_ENV"

      - name: Apply migrations to the ephemeral branch
        run: |
          chmod +x scripts/migrate.sh
          ./scripts/migrate.sh
        env:
          DATABASE_URL: ${{ env.CI_DB_URL }}

      - name: Load seed fixtures onto the ephemeral branch
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          PGOPTIONS="-c search_path=business_app_core,public" \
            psql -v ON_ERROR_STOP=1 "$CI_DB_URL" -f booking_engine/db/sql/02_seed_data.sql

      - run: pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio

      - name: Run live_db tests
        run: pytest tests/live_db/ -q
        env:
          TEST_DATABASE_URL: ${{ env.CI_DB_URL }}

      - name: Delete ephemeral Neon branch
        if: always()
        run: |
          neonctl branches delete "$CI_BRANCH" --project-id "$NEON_PROJECT_ID" \
            || echo "::warning::Failed to delete ephemeral branch $CI_BRANCH — manual cleanup needed"
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "feat(ci): replace local-container schema clone with ephemeral Neon branch

Matches webapp's ci.yml pattern: each PR run gets its own throwaway
Neon branch copy-on-write from production, migrated and seeded, then
deleted. Real prod-shaped data replaces the pg_dump --schema-only +
local Postgres container mechanism."
```

---

### Task 3: Rewrite `deploy-qa.yml` — validate on tmp branch, then promote

**Files:**
- Modify: `.github/workflows/deploy-qa.yml` (full rewrite)

- [ ] **Step 1: Replace the file contents**

```yaml
name: Deploy QA

# QA release pipeline. Pushes to QA provision a throwaway Neon branch from
# production, apply migrations + seed fixtures to it, and run the live_db
# suite against it. Only if that passes does the real QA branch get
# refreshed from production and migrated, then the QA Fly app deployed.
# Production is never touched by this workflow.

on:
  push:
    branches: [QA]
  workflow_dispatch:

concurrency:
  group: booking-engine-qa-release
  cancel-in-progress: false

jobs:
  unit:
    name: Unit tests (no DB)
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
      - run: pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio
      - name: Run unit tests
        run: pytest tests/voice_gateway/ tests/booking_engine/ -q

  validate-on-tmp-branch:
    name: Validate migrations on ephemeral Neon branch
    runs-on: ubuntu-latest
    timeout-minutes: 12
    env:
      NEON_API_KEY: ${{ secrets.NEON_API_KEY }}
      NEON_PROJECT_ID: ${{ vars.NEON_PROJECT_ID }}
      NEON_PROD_BRANCH: ${{ vars.NEON_PROD_BRANCH || 'production' }}
      QA_DATABASE_URL: ${{ secrets.QA_DATABASE_URL }}
      CI_BRANCH: booking-qa-release-${{ github.run_id }}-${{ github.run_attempt }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip

      - name: Guard - required secrets/vars must be set
        run: |
          missing=0
          for name in NEON_API_KEY NEON_PROJECT_ID NEON_PROD_BRANCH QA_DATABASE_URL; do
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

      - name: Install PostgreSQL 17 client
        run: |
          sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg
          sudo apt-get update -qq
          sudo apt-get install -y -qq postgresql-client-17

      - name: Provision ephemeral Neon branch from production
        run: |
          set -euo pipefail
          neonctl branches create \
            --project-id "$NEON_PROJECT_ID" \
            --parent "$NEON_PROD_BRANCH" \
            --name "$CI_BRANCH" >/dev/null
          role=$(python3 -c "import os,urllib.parse as up; print(up.urlparse(os.environ['QA_DATABASE_URL']).username)")
          db=$(python3 -c "import os,urllib.parse as up; print(up.urlparse(os.environ['QA_DATABASE_URL']).path.lstrip('/').split('?')[0])")
          conn=$(neonctl connection-string "$CI_BRANCH" \
            --project-id "$NEON_PROJECT_ID" \
            --role-name "$role" \
            --database-name "$db")
          if [ -z "$conn" ]; then
            echo "::error::Empty connection string for branch $CI_BRANCH"
            exit 1
          fi
          echo "::add-mask::$conn"
          echo "CI_DB_URL=$conn" >> "$GITHUB_ENV"

      - name: Apply migrations to the ephemeral branch
        run: |
          chmod +x scripts/migrate.sh
          ./scripts/migrate.sh
        env:
          DATABASE_URL: ${{ env.CI_DB_URL }}

      - name: Load seed fixtures onto the ephemeral branch
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          PGOPTIONS="-c search_path=business_app_core,public" \
            psql -v ON_ERROR_STOP=1 "$CI_DB_URL" -f booking_engine/db/sql/02_seed_data.sql

      - run: pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio

      - name: Run live_db tests
        run: pytest tests/live_db/ -q
        env:
          TEST_DATABASE_URL: ${{ env.CI_DB_URL }}

      - name: Delete ephemeral Neon branch
        if: always()
        run: |
          neonctl branches delete "$CI_BRANCH" --project-id "$NEON_PROJECT_ID" \
            || echo "::warning::Failed to delete ephemeral branch $CI_BRANCH — manual cleanup needed"

  promote-qa:
    name: Refresh and migrate real QA branch
    needs: [unit, validate-on-tmp-branch]
    runs-on: ubuntu-latest
    timeout-minutes: 10
    env:
      NEON_API_KEY: ${{ secrets.NEON_API_KEY }}
      NEON_PROJECT_ID: ${{ vars.NEON_PROJECT_ID }}
      NEON_PROD_BRANCH: ${{ vars.NEON_PROD_BRANCH || 'production' }}
      NEON_QA_BRANCH: ${{ vars.NEON_QA_BRANCH || 'QA' }}
    steps:
      - uses: actions/checkout@v4

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
    needs: promote-qa
    runs-on: ubuntu-latest
    timeout-minutes: 10
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

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-qa.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy-qa.yml
git commit -m "feat(ci): gate QA release behind ephemeral-branch validation

deploy-qa.yml now validates migrations + live_db tests on a throwaway
Neon branch cloned from production before ever restoring/migrating
the real QA branch, matching the tmp-branch-first pattern used for PR
CI. Real QA is only touched once validation passes."
```

---

### Task 4: Rewrite `deploy-fly-prod.yml` — validate on tmp branch, then migrate prod

**Files:**
- Modify: `.github/workflows/deploy-fly-prod.yml` (full rewrite)

- [ ] **Step 1: Replace the file contents**

```yaml
name: Deploy Fly Production

# Production Fly release pipeline for the voice-webhook/MCP surface
# (kairo-booking-engine). Pushes to main provision a throwaway Neon branch
# from production, apply migrations + seed fixtures to it, and run the
# live_db suite against it. Only if that passes are migrations applied for
# real to production, then the app deployed. A regression or failing
# migration stops the release before prod is touched.

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: booking-engine-prod-release
  cancel-in-progress: false

jobs:
  unit:
    name: Unit tests (no DB)
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
      - run: pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio
      - name: Run unit tests
        run: pytest tests/voice_gateway/ tests/booking_engine/ -q

  validate-on-tmp-branch:
    name: Validate migrations on ephemeral Neon branch
    runs-on: ubuntu-latest
    timeout-minutes: 12
    env:
      NEON_API_KEY: ${{ secrets.NEON_API_KEY }}
      NEON_PROJECT_ID: ${{ vars.NEON_PROJECT_ID }}
      NEON_PROD_BRANCH: ${{ vars.NEON_PROD_BRANCH || 'production' }}
      QA_DATABASE_URL: ${{ secrets.QA_DATABASE_URL }}
      CI_BRANCH: booking-prod-release-${{ github.run_id }}-${{ github.run_attempt }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip

      - name: Guard - required secrets/vars must be set
        run: |
          missing=0
          for name in NEON_API_KEY NEON_PROJECT_ID NEON_PROD_BRANCH QA_DATABASE_URL; do
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

      - name: Install PostgreSQL 17 client
        run: |
          sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg
          sudo apt-get update -qq
          sudo apt-get install -y -qq postgresql-client-17

      - name: Provision ephemeral Neon branch from production
        run: |
          set -euo pipefail
          neonctl branches create \
            --project-id "$NEON_PROJECT_ID" \
            --parent "$NEON_PROD_BRANCH" \
            --name "$CI_BRANCH" >/dev/null
          role=$(python3 -c "import os,urllib.parse as up; print(up.urlparse(os.environ['QA_DATABASE_URL']).username)")
          db=$(python3 -c "import os,urllib.parse as up; print(up.urlparse(os.environ['QA_DATABASE_URL']).path.lstrip('/').split('?')[0])")
          conn=$(neonctl connection-string "$CI_BRANCH" \
            --project-id "$NEON_PROJECT_ID" \
            --role-name "$role" \
            --database-name "$db")
          if [ -z "$conn" ]; then
            echo "::error::Empty connection string for branch $CI_BRANCH"
            exit 1
          fi
          echo "::add-mask::$conn"
          echo "CI_DB_URL=$conn" >> "$GITHUB_ENV"

      - name: Apply migrations to the ephemeral branch
        run: |
          chmod +x scripts/migrate.sh
          ./scripts/migrate.sh
        env:
          DATABASE_URL: ${{ env.CI_DB_URL }}

      - name: Load seed fixtures onto the ephemeral branch
        run: |
          export PATH="/usr/lib/postgresql/17/bin:$PATH"
          PGOPTIONS="-c search_path=business_app_core,public" \
            psql -v ON_ERROR_STOP=1 "$CI_DB_URL" -f booking_engine/db/sql/02_seed_data.sql

      - run: pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio

      - name: Run live_db tests
        run: pytest tests/live_db/ -q
        env:
          TEST_DATABASE_URL: ${{ env.CI_DB_URL }}

      - name: Delete ephemeral Neon branch
        if: always()
        run: |
          neonctl branches delete "$CI_BRANCH" --project-id "$NEON_PROJECT_ID" \
            || echo "::warning::Failed to delete ephemeral branch $CI_BRANCH — manual cleanup needed"

  migrate-prod:
    name: Migrate production DB
    needs: [unit, validate-on-tmp-branch]
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
    timeout-minutes: 10
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

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-fly-prod.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy-fly-prod.yml
git commit -m "feat(ci): gate prod release behind ephemeral-branch validation

deploy-fly-prod.yml now validates migrations + live_db tests on a
throwaway Neon branch cloned from production before applying
migrations to real production, matching deploy-qa.yml's new shape."
```

---

### Task 5: Delete the superseded Lambda deploy path

**Files:**
- Delete: `.github/workflows/deploy.yml`
- Delete: `lambda_handler.py`
- Delete: `booking_engine/Dockerfile`
- Delete: `scripts/deploy-booking.sh`
- Delete: `tests/booking_engine/test_lambda_handler.py`
- Modify: `booking_engine/requirements.txt` (drop `mangum`)

`docs/DEPLOY_READINESS_BRIEF.md` already records "Deploy to Fly.io
(decided)" superseding the Lambda plan. `fly.toml` (app
`kairo-booking-engine`) already builds `booking_engine/Dockerfile.fly` and
is what `deploy-fly-prod.yml` deploys — `booking_engine/Dockerfile` (no
`.fly` suffix) is the old Lambda container image, used only by
`scripts/deploy-booking.sh`, and has no other purpose once that script is
gone. `mangum` (the Lambda ASGI adapter) is imported only by
`lambda_handler.py`.

- [ ] **Step 1: Delete the files**

```bash
git rm .github/workflows/deploy.yml lambda_handler.py booking_engine/Dockerfile scripts/deploy-booking.sh tests/booking_engine/test_lambda_handler.py
```

- [ ] **Step 2: Remove the `mangum` dependency**

In `booking_engine/requirements.txt`, delete the line:

```
mangum>=0.17
```

- [ ] **Step 3: Confirm nothing else references the deleted files**

Run: `grep -rln "lambda_handler\|mangum\|deploy-booking" --include="*.py" --include="*.yml" --include="*.sh" . | grep -v node_modules | grep -v .worktrees`
Expected: no output (or only historical docs under `docs/superpowers/plans/`, which are left untouched as a historical record).

- [ ] **Step 4: Run the unit test suite**

Run: `pytest tests/voice_gateway/ tests/booking_engine/ -q`
Expected: all tests pass (one fewer test file collected than before — `test_lambda_handler.py` is gone).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove superseded AWS Lambda deploy path

Booking Engine deploys to Fly.io (booking_engine/Dockerfile.fly, via
deploy-fly-prod.yml / deploy-qa.yml) — the Lambda path was already
dead per DEPLOY_READINESS_BRIEF.md's 'Deploy to Fly.io (decided)' note
but was never removed. Deletes deploy.yml, lambda_handler.py, the
Lambda-only Dockerfile, deploy-booking.sh, its test, and the mangum
dependency."
```

---

### Task 6: Update README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Fix the architecture diagram**

Change:

```
                              Booking Engine (AWS Lambda)
```

to:

```
                              Booking Engine (Fly.io)
```

- [ ] **Step 2: Fix the Booking Engine description paragraph**

Change:

```
**Booking Engine** — Stateless REST API for shops, staff, services, customers, availability, and appointments. Backed by Neon PostgreSQL via asyncpg. Deployed as an AWS Lambda container with Mangum ASGI adapter and Function URL (~300ms cold start, $0 idle).
```

to:

```
**Booking Engine** — Stateless REST API for shops, staff, services, customers, availability, and appointments. Backed by Neon PostgreSQL via asyncpg. Deployed on Fly.io with auto-stop machines ($0 idle).
```

- [ ] **Step 3: Fix the Project Structure block**

Change:

```
├── config.py            # Settings (DATABASE_URL, pool sizes)
├── requirements.txt     # Service dependencies
└── Dockerfile           # AWS Lambda container image
```

to:

```
├── config.py            # Settings (DATABASE_URL, pool sizes)
├── requirements.txt     # Service dependencies
└── Dockerfile.fly       # Fly.io container image
```

Remove the line (a few lines below, before `fly.toml`):

```
lambda_handler.py        # Mangum entry point for AWS Lambda
```

Change:

```
scripts/
├── setup_neon.sh        # Initialize Neon database (schema + seed)
├── deploy-booking.sh    # Deploy Booking Engine to AWS Lambda
└── deploy-voice.sh      # Deploy Voice Gateway to Fly.io
```

to:

```
scripts/
├── setup_neon.sh        # Initialize Neon database (schema + seed)
├── migrate.sh            # Apply booking_engine/db/sql migrations to a Neon branch
└── deploy-voice.sh      # Deploy Voice Gateway to Fly.io
```

- [ ] **Step 4: Fix the Deployment section**

Change:

```
## Deployment

### Booking Engine → AWS Lambda

```bash
AWS_REGION=eu-central-1 DATABASE_URL=postgresql://... ./scripts/deploy-booking.sh
```

Creates ECR repo, IAM role, Lambda function, and public Function URL on first run. Subsequent runs just update the container image.

### Voice Gateway → Fly.io

```bash
fly auth login
./scripts/deploy-voice.sh
fly secrets set OPENAI_KEY=sk-... BOOKING_ENGINE_URL=https://xxx.lambda-url.eu-central-1.on.aws/
```
```

to:

```
## Deployment

Both services deploy to Fly.io via GitHub Actions on push to `main`
(production) or `QA` (`.github/workflows/deploy-fly-prod.yml` /
`deploy-qa.yml`) — `flyctl deploy` with `fly.toml` / `fly.qa.toml`. Manual
deploys:

```bash
fly auth login
flyctl deploy --config fly.toml       # Booking Engine (production)
./scripts/deploy-voice.sh             # Voice Gateway
```
```

- [ ] **Step 5: Fix the Cost table**

Change:

```
| Component | Idle | Active |
|-----------|------|--------|
| Lambda (Booking Engine) | $0 | ~$0-1/mo |
| Fly.io (Voice Gateway) | $0 | $0 (free tier) |
| Neon PostgreSQL | $0 | $0 (free tier) |
| **Total infrastructure** | **$0** | **$0-1/mo** |
```

to:

```
| Component | Idle | Active |
|-----------|------|--------|
| Fly.io (Booking Engine) | $0 | $0 (free tier) |
| Fly.io (Voice Gateway) | $0 | $0 (free tier) |
| Neon PostgreSQL | $0 | $0 (free tier) |
| **Total infrastructure** | **$0** | **$0** |
```

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: update README for Fly.io Booking Engine deploy, remove Lambda"
```

---

### Task 7: Final verification

- [ ] **Step 1: Confirm all three workflow YAML files parse**

Run:
```bash
for f in .github/workflows/ci.yml .github/workflows/deploy-qa.yml .github/workflows/deploy-fly-prod.yml; do
  python3 -c "import yaml; yaml.safe_load(open('$f'))" && echo "OK: $f"
done
```
Expected: `OK: <path>` printed three times, no errors.

- [ ] **Step 2: Confirm the Lambda workflow is gone and no stray references remain**

Run: `ls .github/workflows/ && grep -rln "AWS Lambda\|lambda_handler\|deploy-booking" README.md .github/workflows/ 2>/dev/null`
Expected: `ls` shows exactly `ci.yml deploy-fly-prod.yml deploy-qa.yml`; the `grep` prints no output.

- [ ] **Step 3: Run the full local unit suite one more time**

Run: `pip install -r booking_engine/requirements.txt pytest pytest-asyncio httpx anyio -q && pytest tests/voice_gateway/ tests/booking_engine/ -q`
Expected: all tests pass.

- [ ] **Step 4: Push and watch the real CI run**

This is the first point real GitHub Actions infrastructure (Neon branch
creation, GitHub secrets, a real Fly deploy) is exercised — confirm with
the user before pushing, since it consumes cloud resources and — for a
push to `QA` — triggers a real deploy.

```bash
git push origin QA
gh run watch --exit-status
```

If `db-tests` / `validate-on-tmp-branch` fails, fetch the failure log
before re-diagnosing:

```bash
gh run list --branch QA --limit 5
gh run view <run-id> --log-failed
```
