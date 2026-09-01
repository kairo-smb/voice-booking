-- Owner-configured rules that send without a human in the loop.
--
-- This is the one place in the product where a message goes out with nobody
-- looking at it, which is why the rails live in the same row as the rule: a
-- kill switch, a weekly ceiling, and the quality-rating pause. An unattended
-- sender is precisely the thing that will not notice its own quality falling.
--
-- Additive and idempotent. Absent rows mean "off" — a shop that has never
-- opened this screen sends nothing, which is the correct default for a feature
-- that messages customers by itself.

CREATE TABLE IF NOT EXISTS whatsapp.automation_rules (
  shop_id      uuid NOT NULL,
  rule_key     text NOT NULL CHECK (rule_key IN ('feedback', 'reminder')),
  enabled      boolean NOT NULL DEFAULT false,
  -- feedback: {"days_after": 2}    reminder: {"hours_before": 24}
  params       jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- Per-rule ceiling. A bug costs one week's cap, not a quality rating.
  weekly_cap   integer NOT NULL DEFAULT 200,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shop_id, rule_key)
);

-- Idempotency for the tick. Without this, a tick that crashes after sending
-- but before committing re-sends on the next run — and "we reminded you"
-- twice is the complaint this whole feature is supposed to avoid.
CREATE TABLE IF NOT EXISTS whatsapp.automation_sends (
  shop_id        uuid NOT NULL,
  rule_key       text NOT NULL,
  appointment_id uuid NOT NULL,
  sent_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (rule_key, appointment_id)
);

CREATE INDEX IF NOT EXISTS automation_sends_shop_week_idx
  ON whatsapp.automation_sends (shop_id, rule_key, sent_at DESC);
