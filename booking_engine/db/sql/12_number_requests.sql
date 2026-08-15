-- Regulatory bundle lifecycle for self-service number provisioning.
-- See docs/number-provisioning-design.md §5. Idempotent: migrate.sh re-applies
-- every file on every run.

CREATE TABLE IF NOT EXISTS voice_agent.number_requests (
  shop_id           uuid PRIMARY KEY REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  status            text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','evaluating','pending_review',
                                      'approved','rejected','provisioned')),
  regulation_sid    text,
  bundle_sid        text,
  end_user_sid      text,
  document_sid      text,
  business_name     text,
  contact_email     text,
  evaluation_errors jsonb,
  rejection_reason  text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  submitted_at      timestamptz,
  reviewed_at       timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Only pending_review rows are polled by the hourly tick; keep that scan cheap.
CREATE INDEX IF NOT EXISTS number_requests_pending_idx
  ON voice_agent.number_requests (status) WHERE status = 'pending_review';

-- Health semaphore. 'unknown' until the first check runs. A Twilio outage
-- leaves the previous value rather than painting every shop red.
ALTER TABLE voice_agent.shop_telephony
  ADD COLUMN IF NOT EXISTS health_status text NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS health_detail text,
  ADD COLUMN IF NOT EXISTS health_checked_at timestamptz;

-- ADD CONSTRAINT has no IF NOT EXISTS, and this file is re-applied every run.
DO $$ BEGIN
  ALTER TABLE voice_agent.shop_telephony
    ADD CONSTRAINT shop_telephony_health_status_check
    CHECK (health_status IN ('unknown','green','red'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
