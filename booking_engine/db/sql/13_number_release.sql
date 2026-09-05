-- Number release: grace period after a subscription lapses, then hand the
-- number back to Twilio. See CLAUDE.md 2026-08-15.

-- Set by the tick the first time it sees a shop that holds a number but has no
-- active plan; cleared if the plan returns before the deadline. Deliberately
-- here rather than a plan_lapsed_at column in business_app_core, which the
-- webapp repo owns.
ALTER TABLE voice_agent.shop_telephony
  ADD COLUMN IF NOT EXISTS release_scheduled_at timestamptz;

-- A released shop keeps its request row so the history survives, and so a
-- later re-provision starts from a known state rather than a missing one.
ALTER TABLE voice_agent.number_requests
  ADD COLUMN IF NOT EXISTS released_at   timestamptz,
  ADD COLUMN IF NOT EXISTS released_number text;

-- Widen the status CHECK (created inline in migration 12) to accept
-- 'released'. ADD CONSTRAINT has no IF NOT EXISTS and this file is
-- re-applied every run, so drop-then-add is wrapped to tolerate both a
-- fresh DB (constraint doesn't exist yet under either name) and a repeat
-- run (already widened). The name below
-- (number_requests_status_check) was confirmed against a scratch DB built
-- from migrations 12+13 via:
--   SELECT conname FROM pg_constraint
--   WHERE conrelid = 'voice_agent.number_requests'::regclass AND contype = 'c';
-- — Postgres's default generated name for a column CHECK declared inline
-- in a CREATE TABLE, matching the column name it constrains.
DO $$ BEGIN
  ALTER TABLE voice_agent.number_requests DROP CONSTRAINT number_requests_status_check;
EXCEPTION WHEN undefined_object THEN NULL; END $$;

ALTER TABLE voice_agent.number_requests
  ADD CONSTRAINT number_requests_status_check
  CHECK (status IN ('draft','evaluating','pending_review',
                     'approved','rejected','provisioned','released'));
