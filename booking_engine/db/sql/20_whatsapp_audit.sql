-- Who-did-what for WhatsApp: an action log for the WABA-affecting operations
-- (campaign enqueue/cancel, automation config, onboarding steps, template
-- ensure) so a failed or surprising send can be inspected after the fact —
-- which surface, which staff member, what upstream said back.
--
-- This is the send-*action* audit. The per-message send/delivery trail already
-- lives in whatsapp.outbound_messages (migration 14/15/16); the new
-- `initiated_by` column on it (below) records who queued each message. Tick and
-- webhook paths write NULL/are trailed by that table — they need no rows here.
--
-- Values (documented, not CHECK-constrained: a violation on a fail-open insert
-- would silently drop the observability row this table exists to keep):
--   event  = campaign.enqueue | campaign.cancel | automation.config |
--            onboarding.start | onboarding.complete | onboarding.abort |
--            templates.ensure
--   source = composer | touchpoint | offer | NULL   (webapp surface)
--   status = success | error | unknown
-- request/response are sanitized: never recipient lists, never tokens, never
-- the single-use onboarding `code`.

CREATE TABLE IF NOT EXISTS whatsapp.audit_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  shop_id uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  -- Acting staff id. NO FK, deliberately: a deleted/unknown staff row must
  -- never take the audit trail down with it (an FK would turn a fail-open
  -- insert into a dropped row). NULL = the tick/system acted.
  actor_id uuid,
  source text,
  event text NOT NULL,
  campaign_key text,
  template_name text,
  is_template boolean,
  recipient_count integer,
  status text,
  http_status integer,
  error_message text,
  request jsonb,
  response jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS whatsapp_audit_shop_event_idx
  ON whatsapp.audit_events (shop_id, event, created_at DESC);

-- Who queued each send on the per-message trail. NULL for the tick's
-- automation sends (there is no human; automation_sends logs those fires).
ALTER TABLE whatsapp.outbound_messages ADD COLUMN IF NOT EXISTS initiated_by uuid;
