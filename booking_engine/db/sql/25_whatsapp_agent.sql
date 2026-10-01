-- The WhatsApp booking agent: opting in, and naming the third writer.
-- See docs/2026-09-21-whatsapp-conversations-design.md.
-- Idempotent: migrate.sh re-applies every file.

-- OFF unless the salon asked for it. A shop that has not requested a robot
-- must never get one, so the default is the refusal and turning it on is an
-- explicit act. Lives on shop_config beside the voice agent's own knobs
-- because it is the same question asked of a different channel.
ALTER TABLE voice_agent.shop_config
  ADD COLUMN IF NOT EXISTS whatsapp_agent_enabled boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN voice_agent.shop_config.whatsapp_agent_enabled IS
  'Opt-in for the WhatsApp booking agent. Default false: silence is the '
  'default, speech is the request.';

-- Three writers share one thread and only one of them is ours.
--
-- Migration 24 named two: 'kairo' (the owner replying from the webapp) and
-- 'phone' (the same owner answering from the WhatsApp Business App, which Meta
-- reports back as an echo). The agent was the third, and until now it had no
-- way to say so — its replies would have landed as 'kairo', indistinguishable
-- from the owner's own.
--
-- That conflation is not cosmetic. 'a human replied, so the agent stands down'
-- is read off these rows; with the agent writing 'kairo' it would read its own
-- last reply as the owner taking over and silence itself after one turn. A new
-- legal value, not a new column: the rows already carry the fact, they just
-- could not spell it.
ALTER TABLE whatsapp.outbound_messages DROP CONSTRAINT IF EXISTS outbound_origin_check;
ALTER TABLE whatsapp.outbound_messages ADD CONSTRAINT outbound_origin_check
  CHECK (origin IN ('kairo','phone','agent'));

COMMENT ON COLUMN whatsapp.outbound_messages.origin IS
  'Who wrote it: kairo = the owner via the webapp, phone = the owner via the '
  'WhatsApp Business App (a Meta echo), agent = the booking agent. The '
  'handover rule reads this column: kairo/phone mean a human took the thread.';
