# Neon ephemeral-branch CI/CD — design

## Why

voice-booking shares its Neon database (`business_app_core`) with the
`webapp` and `marketing-engine` repos — this is not an isolated app DB, it's
integrated with sibling services. CI/CD here should validate against the
real, current shape of that shared database before ever touching QA or
production, the same way `webapp`'s `ci.yml` already does.

Today it doesn't: `ci.yml`, `deploy-qa.yml`, and `deploy-fly-prod.yml` each
run a `db-tests` job that `pg_dump --schema-only` clones the schema into a
local Postgres container (structure only, no data) and seeds it with fake
fixture data (`booking_engine/db/sql/02_seed_data.sql`). Since
`2026-07-16` every CI/QA/prod run has failed at that seeding step:

```
ERROR: there is no unique or exclusion constraint matching the ON
CONFLICT specification
psql:booking_engine/db/sql/02_seed_data.sql:75
```

**Confirmed root cause** (checked directly against the real Neon schema,
project `kairo`, shared by voice-booking/webapp/marketing-engine):
`business_app_core.staff_schedules` has only a plain (non-unique) index on
`(staff_id, day_of_week)` — `idx_staff_schedules_staff_day`. The matching
`UNIQUE (staff_id, day_of_week)` constraint exists only in the local
bootstrap schema (`01_schema.sql`), never in real Neon. Line 75's
`ON CONFLICT (staff_id, day_of_week) DO NOTHING` is syntactically invalid
against a table with no constraint matching that column list — it fails
every time, regardless of whether an actual duplicate exists. Every other
`ON CONFLICT` target in the same file (`shops.id`, `staff.id`,
`services.id`, `staff_services(staff_id, service_id)`, `customers.id`,
`phone_contacts(phone_number, customer_id)`) has a matching real unique
index; `staff_schedules` is the only broken one. This has blocked every
QA and prod deploy since 07-16.

Per `CLAUDE.md`'s standing constraint ("never change the shared schema;
revise the schema only if truly impossible"), the fix is not a migration
adding the missing constraint — it's a one-clause rewrite of
`02_seed_data.sql` line 71-75 to a constraint-independent equivalent:

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

Same idempotent "skip if already present" semantics as
`ON CONFLICT ... DO NOTHING`, but works against any schema regardless of
whether a matching unique constraint exists — correct for both the local
bootstrap schema and the real Neon-shaped branches this design introduces
below. No other line in the file needs to change.

## Architecture

Replace the local-container-clone-and-fake-seed mechanism with real,
throwaway Neon branches — copy-on-write children of `production` — carrying
real prod-shaped data. Apply the repo's actual migrations
(`booking_engine/db/sql/03+`, via `scripts/migrate.sh`) to the branch, run
`tests/live_db/` against it, then delete it. Only after that validation
passes does a workflow touch the real target (QA branch or production).
This is the same pattern `webapp/ci.yml` already uses for PRs; this design
extends it to voice-booking's PR, QA-release, and prod-release flows alike.

**Known limitation:** the ephemeral branch is a point-in-time
copy-on-write snapshot taken when it's created — it does not track
production afterward. Between that snapshot and `migrate-prod` actually
running against real production (after `unit` + `validate-on-tmp-branch`
both complete, up to ~12 minutes later), live application traffic keeps
writing to production. If a migration's success were data-dependent (e.g.
a `NOT NULL` backfill or a new uniqueness constraint), it's possible —
though narrow — for validation to pass against the snapshot while the real
`migrate-prod` run hits a conflict from writes made after the snapshot.
This is inherent to snapshot-based pre-flight validation, not something
the `concurrency` group closes (that only serializes workflow runs against
each other, not against live application writes). Ephemeral-branch
validation substantially derisks migrations — it replaced a schema-only,
fake-data local clone with a true copy-on-write clone of prod's real
schema *and* data — but it is not a complete guarantee against migration
failure on real production.

### `ci.yml` (PR into `main`/`QA`)

- `unit` job: unchanged — no DB, runs first, fails fast.
- `db-tests` job, replacing the current local-container approach:
  1. Guard required secrets/vars (`NEON_API_KEY`, `NEON_PROJECT_ID`,
     `NEON_PROD_BRANCH`, `QA_DATABASE_URL` for role/db-name derivation).
  2. `neonctl branches create --parent production --name booking-ci-${{ github.run_id }}-${{ github.run_attempt }}`
     — unique per run+attempt so concurrent PRs (and re-runs) never collide,
     and so it can't collide with webapp's own `ci-${{ run_id }}-...`
     branches in the same Neon project.
  3. Resolve the branch's connection string via `neonctl connection-string`
     (role/db name derived from `QA_DATABASE_URL`, same approach as webapp).
  4. Apply migrations (`scripts/migrate.sh` against that connection string).
  5. Load `02_seed_data.sql` (fixed, see above) into that connection
     string — layers the fixture rows `tests/live_db/*.py` hardcode
     (`SHOP_ID`, `PHONE_MARIA`, etc.) on top of the real copy-on-write prod
     data, the same role webapp's "sync Stripe test-mode products" step
     plays on its own ephemeral branch.
  6. `pytest tests/live_db/` against that connection string.
  7. Delete the branch — `if: always()`, so a failed run still cleans up.
- The local Postgres service container and `pg_dump` schema-only clone are
  removed — the ephemeral Neon branch replaces them. `02_seed_data.sql`
  itself is kept (with the fix above) and now runs against real
  Neon-shaped data instead of a bare structure-only local clone.

### `deploy-qa.yml` (push to `QA`)

New job order:
1. `unit` — unchanged.
2. `validate-on-tmp-branch` — same create/migrate/test/delete sequence as
   `ci.yml`'s `db-tests` job (branch name
   `booking-qa-release-${{ github.run_id }}`).
3. `promote-qa` (only if validation passed) — today's `refresh-qa-db` +
   `migrate-qa` logic, unchanged: `neonctl branches restore` the real `QA`
   branch from `production`, then apply migrations for real.
4. `deploy-qa` — unchanged (flyctl deploy + health smoke check).

### `deploy-fly-prod.yml` (push to `main`)

Same shape:
1. `unit`
2. `validate-on-tmp-branch` (from `production`)
3. `migrate-prod` (only if validation passed) — apply migrations directly
   to `production`; no restore step, since prod is already the source.
4. `deploy-prod` — unchanged.

Existing `concurrency` groups on the release workflows are kept (prevents
overlapping QA/prod releases). PR-time ephemeral branches need no
concurrency group — each run gets its own isolated branch, same as webapp.

## Lambda removal (in scope, per owner confirmation)

`docs/DEPLOY_READINESS_BRIEF.md` already records "Deploy to Fly.io
(decided)" superseding the earlier Lambda plan, and the original QA/Fly
CI/CD plan explicitly left `deploy.yml` (Lambda) "untouched" rather than
removing it — that gap gets closed here. Delete:

- `.github/workflows/deploy.yml`
- `lambda_handler.py`
- `scripts/deploy-booking.sh`
- `tests/booking_engine/test_lambda_handler.py`
- Lambda-specific sections of `README.md` (architecture diagram line,
  `Dockerfile` reference, `deploy-booking.sh` reference, the "Booking
  Engine → AWS Lambda" how-to section, the Lambda cost line)

Out of scope for this change (flagged, not actioned): the AWS GitHub repo
secrets (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`CONTROL_PLANE_SECRET`, `DEMO_SHOP_ID`) become unused once `deploy.yml` is
gone — deleting repo secrets is a manual, out-of-band action like the
`TWILIO_*` secret additions were, not something done from this repo.

## What doesn't change

- `01_schema.sql` stays untouched — still the from-scratch local bootstrap
  schema. `02_seed_data.sql` gets the one-clause fix above but keeps its
  role as the fixture data both local bootstrap and CI ephemeral branches
  load — it's no longer true that it "never runs against a real Neon
  branch" (CI now runs it deliberately, as a fixture-sync step, not as a
  schema-migration step), but nothing about *how* local dev uses it
  changes.
- `scripts/migrate.sh` is unchanged — it still skips `01_schema.sql` and
  `02_seed_data.sql` when applying real migrations to QA/production. The
  new CI/release workflows call `02_seed_data.sql` directly as their own
  explicit fixture-sync step on the throwaway branch only, never through
  `migrate.sh` and never against QA or production.
- `tests/live_db/conftest.py`'s production-host guard
  (`_PROD_HOST_FRAGMENT`) stays as a defense-in-depth check — the ephemeral
  branch gets its own endpoint hostname distinct from prod's, but the
  refusal-to-run-against-prod guard is cheap insurance worth keeping.
- Fixture UUIDs in `tests/live_db/*.py` (`SHOP_ID`, `STAFF_MIRCO`, etc.)
  keep working unchanged, but not because they pre-exist in prod — they
  don't (verified: 0 rows in production for `SHOP_ID`, which has 4 real
  shops of its own). They keep working because the seed step above still
  runs, layering these exact fixture rows onto the ephemeral branch after
  cloning from prod — same mechanism as today, just against real
  Neon-shaped data instead of a bare local schema clone.
