-- Meta's own limits, recorded so they can be *enforced* rather than assumed.
--
-- Migration 15 stored `messaging_limit` from the phone number's `throughput`
-- level, which is the wrong field: throughput is messages/second, the
-- messaging limit is business-initiated conversations per rolling 24h. They
-- are different ceilings with different consequences and nothing was reading
-- either of them. See services/messaging/meta_limits.py.
--
-- Idempotent: migrate.sh re-applies every file.

-- Messages/second Meta allows for this number ('STANDARD' | 'HIGH'). Ignored
-- for a coexistence number, which Meta pins at 20 mps regardless.
ALTER TABLE whatsapp.senders ADD COLUMN IF NOT EXISTS throughput_level text;

-- `messaging_limit` now holds Meta's volume tier ('TIER_250', 'TIER_1K', …),
-- read from the phone number's `messaging_limit_tier`. Any pre-existing value
-- came from `throughput.level` and means something else entirely; clearing it
-- makes meta_limits.tier_daily_conversations() fail closed to the unverified
-- 250 until the next sweep reads the real tier back.
UPDATE whatsapp.senders
   SET messaging_limit = NULL
 WHERE messaging_limit IS NOT NULL
   AND messaging_limit NOT LIKE 'TIER\_%';

COMMENT ON COLUMN whatsapp.senders.messaging_limit IS
  'Meta volume tier (TIER_250, TIER_1K, …): business-initiated conversations '
  'per ROLLING 24h. NULL means unknown -> treated as the unverified 250.';
COMMENT ON COLUMN whatsapp.senders.daily_cap IS
  'Kairo''s drip rate. Only ever narrows Meta''s tier, never widens it — see '
  'meta_limits.effective_daily_cap().';

-- The Meta ceiling is checked against a rolling 24h window, so this index has
-- to serve `sent_at >= now() - 24h` and not just the calendar-day count.
CREATE INDEX IF NOT EXISTS whatsapp_outbound_shop_sent_at_idx
  ON whatsapp.outbound_messages (shop_id, sent_at DESC)
  WHERE sent_at IS NOT NULL;

-- "Has this customer heard from us recently?" — our own guard against Meta's
-- per-user cross-brand marketing cap (131049), which is otherwise only
-- discoverable after burning the send.
CREATE INDEX IF NOT EXISTS whatsapp_outbound_customer_sent_idx
  ON whatsapp.outbound_messages (shop_id, customer_id, sent_at DESC)
  WHERE sent_at IS NOT NULL AND customer_id IS NOT NULL;
