-- Migration 09: shop_telephony.provider defaults to 'twilio'
--
-- Telnyx -> Twilio migration. Existing rows are untouched; nothing is live
-- yet. See docs/superpowers/specs/2026-07-16-telnyx-to-twilio-migration-design.md
-- Idempotent — safe to re-run.
--
-- Apply to Neon BEFORE deploying the code that assumes this default:
--   psql "$DATABASE_URL" -f booking_engine/db/sql/09_shop_telephony_twilio_provider.sql

ALTER TABLE voice_agent.shop_telephony
  ALTER COLUMN provider SET DEFAULT 'twilio';
