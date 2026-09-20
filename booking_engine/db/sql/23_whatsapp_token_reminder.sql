-- The renewal nudge stops being pull-only.
--
-- Migration 22 recorded *when* the salon's business token dies. The only
-- warning built on it was a banner, which the owner has to open the app to
-- see — in the one week that matters, and not at all if their session is in
-- employee view, since every WhatsApp route is owner-only. The 2026-09-04
-- entry said as much and called email "the agreed next step, not built".
--
-- On 2026-09-20 the first real onboarding came back with `token_expires_at`
-- exactly 60 days out: the expiry is a measured fact now, not an inference
-- from a configuration name. So the email is built, and this column is what
-- keeps it from becoming noise — the hourly tick would otherwise send one
-- every hour for the seven days of the renewal window.
--
-- It records the attempt, not the delivery: a shop with no owner mailbox, or
-- a Resend refusal, must not put the tick into an hourly retry loop against a
-- fact about the shop. The banner is still there for both cases.
--
-- NULL means never nudged, which is the truth for every existing row.
--
-- Idempotent: migrate.sh re-applies every file.

ALTER TABLE whatsapp.senders
  ADD COLUMN IF NOT EXISTS token_reminder_sent_at timestamptz;

COMMENT ON COLUMN whatsapp.senders.token_reminder_sent_at IS
  'When the tick last asked the webapp to email this salon about the expiring '
  'business token. Records the attempt, not the delivery — it exists to stop '
  'an hourly resend across the renewal window, not to prove a mailbox was '
  'reached. NULL = never nudged.';

-- Superseded: the token is encrypted at rest as of 2026-09-20 (Fernet, key in
-- the app's secret store — see services/secret_box.py). Sealed values carry a
-- `v1:` prefix; anything without one is a row written before the key existed.
COMMENT ON COLUMN whatsapp.senders.access_token IS
  'Customer-scoped business token from Embedded Signup''s code exchange. The '
  'only credential for this WABA — there is no shared parent. May expire; see '
  'token_expires_at. Encrypted at rest: a `v1:` prefix marks a sealed value, '
  'anything else is legacy plaintext and is re-sealed on the next write.';
