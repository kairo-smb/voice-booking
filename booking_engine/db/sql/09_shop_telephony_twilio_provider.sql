-- 09: shop_telephony.provider now defaults to 'twilio' (Telnyx -> Twilio
-- migration). Existing rows are untouched; nothing is live yet — see
-- docs/superpowers/specs/2026-07-16-telnyx-to-twilio-migration-design.md
ALTER TABLE voice_agent.shop_telephony
  ALTER COLUMN provider SET DEFAULT 'twilio';
