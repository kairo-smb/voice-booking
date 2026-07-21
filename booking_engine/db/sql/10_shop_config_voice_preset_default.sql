-- Migration 10: fix voice_preset default/values after voice_preset vocabulary change
--
-- Migration 04 defaulted voice_agent.shop_config.voice_preset to 'warm_female'
-- (old display-preset vocabulary). The app now uses OpenAI Realtime voice names
-- directly (alloy/ash/ballad/coral/echo/sage/shimmer/verse), default 'verse'.
-- The old default was never a hard failure (the code falls back to 'verse' for
-- any unrecognized value), but it left the column default and any pre-existing
-- rows out of sync with reality. Idempotent — safe to re-run.
--
-- Apply to Neon BEFORE deploying the code that reads/writes this column:
--   psql "$DATABASE_URL" -f booking_engine/db/sql/10_shop_config_voice_preset_default.sql

ALTER TABLE voice_agent.shop_config
  ALTER COLUMN voice_preset SET DEFAULT 'verse';

UPDATE voice_agent.shop_config
  SET voice_preset = 'verse'
  WHERE voice_preset IN ('warm_female', 'neutral_female', 'neutral_male');
