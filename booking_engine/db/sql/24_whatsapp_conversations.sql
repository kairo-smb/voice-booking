-- WhatsApp becomes two-way. See docs/2026-09-21-whatsapp-conversations-design.md.
-- Idempotent: migrate.sh re-applies every file.

-- Who wrote it, and what we made of it.
ALTER TABLE whatsapp.inbound_messages
  ADD COLUMN IF NOT EXISTS customer_id   uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS wa_message_id text,
  ADD COLUMN IF NOT EXISTS transcript    text,
  ADD COLUMN IF NOT EXISTS intent        text,
  ADD COLUMN IF NOT EXISTS confidence    numeric,
  ADD COLUMN IF NOT EXISTS summary       text,
  ADD COLUMN IF NOT EXISTS read_at       timestamptz;

-- Meta retries webhooks. Without this a retry is a duplicate in the thread.
CREATE UNIQUE INDEX IF NOT EXISTS inbound_messages_wa_id_uniq
  ON whatsapp.inbound_messages (wa_message_id) WHERE wa_message_id IS NOT NULL;

COMMENT ON COLUMN whatsapp.inbound_messages.transcript IS
  'Voice-note transcript. NULL means not transcribed — never overwrites body.';
COMMENT ON COLUMN whatsapp.inbound_messages.intent IS
  'Routing verdict for the session this message belongs to. NULL = not classified.';

-- Every sender is coexistence: the owner also answers from their phone, and
-- Meta reports those as message_echoes. 'phone' rows have no provider status
-- lifecycle and never extend the 24h window.
ALTER TABLE whatsapp.outbound_messages
  ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'kairo';
ALTER TABLE whatsapp.outbound_messages DROP CONSTRAINT IF EXISTS outbound_origin_check;
ALTER TABLE whatsapp.outbound_messages ADD CONSTRAINT outbound_origin_check
  CHECK (origin IN ('kairo','phone'));

-- What the agent must ask before booking a given service. Owner-authored.
-- Not a column on business_app_core.services: that schema belongs to the
-- webapp and this repo does not alter it.
CREATE TABLE IF NOT EXISTS voice_agent.service_intake (
  shop_id    uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  service_id uuid NOT NULL REFERENCES business_app_core.services(id) ON DELETE CASCADE,
  questions  text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shop_id, service_id)
);

-- voice_agent.calls was never telephony-only: it is a session row.
ALTER TABLE voice_agent.calls
  ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'voice';
ALTER TABLE voice_agent.calls DROP CONSTRAINT IF EXISTS calls_channel_check;
ALTER TABLE voice_agent.calls ADD CONSTRAINT calls_channel_check
  CHECK (channel IN ('voice','whatsapp'));
COMMENT ON COLUMN voice_agent.calls.duration_seconds IS
  'Voice only. Meaningless for channel = whatsapp.';
