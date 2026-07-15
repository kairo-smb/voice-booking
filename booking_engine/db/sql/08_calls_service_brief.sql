-- Migration 08: structured hairstylist service brief per call
--
-- Populated at call end by the post-call classifier pass (voice_gateway), which
-- extracts what the customer wants (services, hair history, constraints) from a
-- hairstylist's standpoint. Surfaced in the webapp Inbox / request details.
-- Additive and idempotent — safe to re-run.
--
-- Apply to Neon BEFORE deploying the code that writes this column:
--   psql "$DATABASE_URL" -f booking_engine/db/sql/08_calls_service_brief.sql

ALTER TABLE voice_agent.calls
  ADD COLUMN IF NOT EXISTS service_brief jsonb NOT NULL DEFAULT '{}'::jsonb;
