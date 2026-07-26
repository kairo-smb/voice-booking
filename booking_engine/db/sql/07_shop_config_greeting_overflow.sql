-- Migration 07: per-shop overflow greeting
--
-- Adds a shop-authored greeting used when answer_mode = 'overflow' (the AI is
-- catching calls the salon staff couldn't answer). When empty, prompt_assembler
-- falls back to a sensible Italian default. always_on shops keep using
-- greeting_after_disclosure. Additive and idempotent — safe to re-run.
--
-- Apply to Neon BEFORE deploying the code that writes this column:
--   psql "$DATABASE_URL" -f booking_engine/db/sql/07_shop_config_greeting_overflow.sql

ALTER TABLE voice_agent.shop_config
  ADD COLUMN IF NOT EXISTS greeting_overflow text NOT NULL DEFAULT '';
