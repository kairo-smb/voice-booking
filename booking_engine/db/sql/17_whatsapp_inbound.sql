-- Inbound replies, persisted. The webhook previously logged and discarded
-- them; campaign measurement (design §9) needs "did the recipient reply within
-- 72h" as a queryable signal. A reply is matched back to the message it answers
-- by phone number: the reply's `from_phone` equals the sent message's `to_phone`.
-- Idempotent: replayed on every migrate run.

CREATE TABLE IF NOT EXISTS whatsapp.inbound_messages (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id      uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  from_phone   text NOT NULL,
  body         text NOT NULL DEFAULT '',
  message_type text NOT NULL DEFAULT 'text',
  received_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS inbound_messages_shop_phone_idx
  ON whatsapp.inbound_messages (shop_id, from_phone, received_at DESC);
