-- WhatsApp marketing: one sender (and one WABA, in one Twilio subaccount) per
-- salon, plus the per-salon approved templates and the drip queue.
-- See CLAUDE.md's WhatsApp entry. Idempotent: migrate.sh re-applies every file.

CREATE SCHEMA IF NOT EXISTS whatsapp;

-- One row per shop. `subaccount_sid` exists because Meta/Twilio allow exactly
-- one WABA per Twilio account, so a salon's WABA cannot live in Kairo's
-- parent account alongside every other salon's.
CREATE TABLE IF NOT EXISTS whatsapp.senders (
  shop_id         uuid PRIMARY KEY REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  status          text NOT NULL DEFAULT 'pending_signup'
                  CHECK (status IN ('pending_signup','verifying','online','offline','failed')),
  -- 'kairo'  = reuse voice_agent.shop_telephony.kairo_number (already bought,
  --            already SMS-capable, already under the salon's own bundle)
  -- 'salon'  = the salon's own number, imported
  source          text NOT NULL DEFAULT 'kairo' CHECK (source IN ('kairo','salon')),
  subaccount_sid  text,
  -- Twilio signs a webhook with the auth token of the account that owns the
  -- resource, so subaccount traffic is signed with the subaccount's own
  -- token. Without it here, every genuine WhatsApp webhook fails signature
  -- validation — and those webhooks withdraw marketing consent.
  subaccount_auth_token text,
  waba_id         text,
  sender_sid      text,
  phone_number    text,
  display_name    text NOT NULL,
  quality_rating  text,
  messaging_limit text,
  -- Meta caps an unverified WABA at 250 business-initiated conversations/24h.
  -- 50 is the product default and stays well under it; raising this past the
  -- tier the sender actually has just produces Meta-side failures.
  daily_cap       smallint NOT NULL DEFAULT 50,
  offline_reason  text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  verified_at     timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_senders_phone_uniq
  ON whatsapp.senders (phone_number) WHERE phone_number IS NOT NULL;

-- Templates are per-WABA, so per-subaccount, so per-shop: the same skeleton
-- has a different ContentSid for every salon and must be approved separately.
CREATE TABLE IF NOT EXISTS whatsapp.templates (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id          uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  template_key     text NOT NULL,
  content_sid      text NOT NULL,
  category         text NOT NULL DEFAULT 'MARKETING'
                   CHECK (category IN ('MARKETING','UTILITY','AUTHENTICATION')),
  status           text NOT NULL DEFAULT 'unsubmitted'
                   CHECK (status IN ('unsubmitted','received','pending','approved',
                                     'rejected','paused','disabled')),
  variable_count   smallint NOT NULL DEFAULT 0,
  rejection_reason text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (shop_id, template_key)
);

-- Queue and log in one table: a row is created the moment a send is planned
-- and never deleted, so "why did Giulia not get it?" is always answerable.
CREATE TABLE IF NOT EXISTS whatsapp.outbound_messages (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id           uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  customer_id       uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  campaign_key      text,
  to_phone          text NOT NULL,
  from_number       text NOT NULL,
  content_sid       text NOT NULL,
  variables         jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- The template body with variables substituted. Stored because the
  -- ContentSid alone doesn't tell anyone what the customer actually read.
  preview           text NOT NULL DEFAULT '',
  -- 'sending' is a claim, not a real provider state: the drip sweep flips rows
  -- into it atomically so two overlapping ticks can't both send the same one.
  -- A row stuck there (crash mid-send) is requeued by the next sweep.
  status            text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','sending','sent','delivered','read',
                                      'failed','suppressed','cancelled')),
  suppressed_reason text,
  scheduled_at      timestamptz NOT NULL DEFAULT now(),
  provider_sid      text,
  price_usd         numeric,
  credits_charged   integer,
  error_code        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  sent_at           timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- The drip sweep's only read pattern: what is due, right now.
CREATE INDEX IF NOT EXISTS whatsapp_outbound_due_idx
  ON whatsapp.outbound_messages (scheduled_at)
  WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS whatsapp_outbound_provider_sid_idx
  ON whatsapp.outbound_messages (provider_sid);

-- Counting today's sends against daily_cap.
CREATE INDEX IF NOT EXISTS whatsapp_outbound_shop_sent_idx
  ON whatsapp.outbound_messages (shop_id, sent_at);

-- Idempotency as a DB constraint: one campaign reaches each customer at most
-- once, however many times the enqueue call is retried or double-clicked.
CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_outbound_campaign_customer_uniq
  ON whatsapp.outbound_messages (shop_id, campaign_key, customer_id)
  WHERE campaign_key IS NOT NULL AND customer_id IS NOT NULL;
