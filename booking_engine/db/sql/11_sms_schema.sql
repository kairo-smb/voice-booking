-- SMS marketing sends. See docs/messaging-design.md §4.1.
-- Additive and idempotent: migrate.sh re-applies every file on every run.

CREATE SCHEMA IF NOT EXISTS sms;

CREATE TABLE IF NOT EXISTS sms.campaigns (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id     uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  name        text NOT NULL,
  status      text NOT NULL DEFAULT 'draft'
              CHECK (status IN ('draft','approved','sending','sent','cancelled')),
  approved_at timestamptz,
  approved_by uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sms.outbound_messages (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id       uuid REFERENCES sms.campaigns(id) ON DELETE CASCADE,
  shop_id           uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  customer_id       uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  to_phone          text NOT NULL,
  from_number       text NOT NULL,
  body              text NOT NULL,
  segments          smallint NOT NULL,
  encoding          text NOT NULL CHECK (encoding IN ('gsm7','ucs2')),
  status            text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','sent','delivered','failed','suppressed')),
  suppressed_reason text,
  provider_sid      text,
  price_usd         numeric,
  credits_charged   integer,
  error_code        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  sent_at           timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Idempotency as a DB constraint: a campaign reaches each customer at most once
-- however many times the send job re-runs.
CREATE UNIQUE INDEX IF NOT EXISTS sms_outbound_campaign_customer_uniq
  ON sms.outbound_messages (campaign_id, customer_id)
  WHERE campaign_id IS NOT NULL AND customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS sms_outbound_provider_sid_idx
  ON sms.outbound_messages (provider_sid);

-- Suppression list of last resort: a STOP must be honoured from a phone that
-- matches no customers row (import, wrong number, deleted customer), and this
-- is also the legal evidence trail for the Garante.
CREATE TABLE IF NOT EXISTS sms.opt_outs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id          uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  phone_normalized text NOT NULL,
  keyword          text NOT NULL,
  raw_body         text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (shop_id, phone_normalized)
);
