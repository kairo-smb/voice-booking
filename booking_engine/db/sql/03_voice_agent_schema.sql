-- Voice Agent control-plane schema.
-- Owns call lifecycle data written by the Voice Gateway.
-- Webapp Control Plane reads via HTTP (Booking Engine endpoints), never directly.

-- Additive columns on shops (read by Voice Gateway when building OpenAI session)
-- Tables live in business_app_core schema (shared Neon DB with webapp)
ALTER TABLE business_app_core.shops ADD COLUMN IF NOT EXISTS voice    TEXT NOT NULL DEFAULT 'alloy';
ALTER TABLE business_app_core.shops ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'it';

CREATE SCHEMA IF NOT EXISTS voice_agent;

CREATE TABLE IF NOT EXISTS voice_agent.calls (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id               UUID NOT NULL REFERENCES business_app_core.shops(id),
  twilio_call_sid       TEXT UNIQUE,
  caller_number         TEXT NOT NULL,
  customer_id           UUID REFERENCES business_app_core.customers(id),
  customer_match        TEXT NOT NULL CHECK (customer_match IN ('existing','created','unmatched','ambiguous')),
  started_at            TIMESTAMPTZ NOT NULL,
  ended_at              TIMESTAMPTZ,
  duration_seconds      INTEGER,
  outcome               TEXT CHECK (outcome IN ('booked','rescheduled','cancelled','info','abandoned','escalated','failed')),
  outcome_reason        TEXT,
  summary               TEXT,
  appointment_id        UUID REFERENCES business_app_core.appointments(id),
  requested_service_ids UUID[],
  requested_staff_id    UUID,
  error_code            TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_voice_calls_shop_started  ON voice_agent.calls (shop_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_calls_shop_outcome  ON voice_agent.calls (shop_id, outcome);
CREATE INDEX IF NOT EXISTS idx_voice_calls_customer      ON voice_agent.calls (customer_id);

CREATE TABLE IF NOT EXISTS voice_agent.call_transcripts (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  call_id     UUID NOT NULL REFERENCES voice_agent.calls(id) ON DELETE CASCADE,
  turn_index  INTEGER NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('caller','assistant','system')),
  text        TEXT NOT NULL,
  at          TIMESTAMPTZ NOT NULL,
  UNIQUE (call_id, turn_index)
);

CREATE TABLE IF NOT EXISTS voice_agent.call_events (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  call_id     UUID NOT NULL REFERENCES voice_agent.calls(id) ON DELETE CASCADE,
  at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  type        TEXT NOT NULL,
  payload     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_voice_call_events_call ON voice_agent.call_events (call_id, at);
