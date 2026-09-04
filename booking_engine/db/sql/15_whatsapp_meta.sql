-- WhatsApp moves off Twilio onto Meta Cloud API direct (Tech Provider).
--
-- Migration 14 modelled one Twilio subaccount per salon, because Twilio allows
-- one WABA per account. That whole model is gone: BYO WABA is impossible on
-- Twilio (error 63103 — Twilio must attach the WABA to *its* Meta credit line,
-- and Meta won't release an existing payment method), and Twilio's migration
-- path deletes the salon's WhatsApp Business App. See CLAUDE.md
-- §2026-08-24 and docs/knowledge/api/whatsapp.md.
--
-- Nothing has ever been sent through migration 14's tables, so columns are
-- dropped rather than backfilled. Idempotent: migrate.sh re-applies every file.

-- ------------------------------------------------------------------- senders

-- Twilio-shaped identity, replaced wholesale.
ALTER TABLE whatsapp.senders DROP COLUMN IF EXISTS subaccount_sid;
ALTER TABLE whatsapp.senders DROP COLUMN IF EXISTS subaccount_auth_token;
ALTER TABLE whatsapp.senders DROP COLUMN IF EXISTS sender_sid;

-- Meta's identifiers. `phone_number_id` is what every send and every phone
-- number call is keyed on; `waba_id` (already present) is what webhooks route
-- on and what templates hang off.
ALTER TABLE whatsapp.senders ADD COLUMN IF NOT EXISTS phone_number_id text;

-- The customer-scoped business token from Embedded Signup's code exchange.
-- Unlike the Twilio model there is no shared parent credential: every call
-- into a salon's WABA is authenticated with this.
--
-- This originally claimed the token "does not expire unless the salon revokes
-- our app". That was wrong — expiry is a property of the Login Configuration,
-- and ours issues 60-day tokens. See migration 22 for the correction and
-- `token_expires_at`.
ALTER TABLE whatsapp.senders ADD COLUMN IF NOT EXISTS access_token text;

-- Meta's own answer to "is this number also live on the Business App?", read
-- back from the phone number after onboarding. Recorded because a coexistence
-- number behaves differently (fixed 20 mps, registration already done) and
-- because it is the one fact that proves the salon kept their app.
ALTER TABLE whatsapp.senders ADD COLUMN IF NOT EXISTS platform_type text;

-- 'coexistence' — the salon's existing WhatsApp Business App number, still
--                live on their phone. The path this feature exists for.
-- 'new'        — a fresh WABA on a number not yet on WhatsApp. Falls out of
--                the same Embedded Signup popup, not a second integration.
ALTER TABLE whatsapp.senders DROP CONSTRAINT IF EXISTS senders_source_check;
UPDATE whatsapp.senders SET source = 'coexistence' WHERE source NOT IN ('coexistence','new');
ALTER TABLE whatsapp.senders ALTER COLUMN source SET DEFAULT 'coexistence';
ALTER TABLE whatsapp.senders ADD CONSTRAINT senders_source_check
  CHECK (source IN ('coexistence','new'));

CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_senders_waba_uniq
  ON whatsapp.senders (waba_id) WHERE waba_id IS NOT NULL;

-- ----------------------------------------------------------------- templates

-- Twilio Content SIDs (HX…) become Meta template ids, and Meta sends a
-- template by *name plus language*, not by id — so the name is the load-
-- bearing column and the id is only good for status lookups.
ALTER TABLE whatsapp.templates ADD COLUMN IF NOT EXISTS meta_template_id text;
ALTER TABLE whatsapp.templates ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE whatsapp.templates ADD COLUMN IF NOT EXISTS language text NOT NULL DEFAULT 'it';
ALTER TABLE whatsapp.templates DROP COLUMN IF EXISTS content_sid;

CREATE INDEX IF NOT EXISTS whatsapp_templates_name_idx
  ON whatsapp.templates (name);

-- ---------------------------------------------------------- outbound_messages

ALTER TABLE whatsapp.outbound_messages ADD COLUMN IF NOT EXISTS template_name text;
ALTER TABLE whatsapp.outbound_messages ADD COLUMN IF NOT EXISTS template_language text
  NOT NULL DEFAULT 'it';
ALTER TABLE whatsapp.outbound_messages DROP COLUMN IF EXISTS content_sid;

-- Meta bills the salon directly (Tech Provider has no credit line to share),
-- so nothing here debits AI credits any more and price_usd is our own
-- pre-send estimate, never a provider fact — Meta returns no amount on send
-- and none on the status webhook. Kept so the owner can see expected spend.
COMMENT ON COLUMN whatsapp.outbound_messages.price_usd IS
  'Estimated USD at send time (whatsapp_pricing). Meta never reports an amount.';
COMMENT ON COLUMN whatsapp.outbound_messages.credits_charged IS
  'Unused since the Meta migration: the salon pays Meta directly.';
