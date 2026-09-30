-- Live updates for WhatsApp: bump the webapp's per-shop 'whatsapp' counter on
-- every write to the message tables — the send loop claiming/sending, Meta's
-- status webhooks (delivered/read/failed), inbound messages, enqueue/cancel.
-- The counters, the bump function and the polling live in the webapp
-- (its migration 73_shop_changes.sql, and AGENTS.md 2026-09-30 there).
--
-- Guarded: the function is created by the WEBAPP's migration 73. On a database
-- that doesn't have it yet (this repo's CI branch off production, before the
-- webapp ships), skip with a notice — every migration is replayed on every run,
-- so the next run installs the triggers. In migrate-all the webapp runs first.
--
-- Deadlock rule (from 73): keep writes to these tables short and late in a
-- transaction, and never bump two domains of one shop in opposite orders.
--
-- Idempotent: CREATE OR REPLACE TRIGGER.

DO $$
BEGIN
  IF to_regprocedure('business_app_core.bump_shop_changes()') IS NULL THEN
    RAISE NOTICE '29_shop_changes_whatsapp: business_app_core.bump_shop_changes() not found (webapp migration 73 not applied yet) — triggers skipped';
    RETURN;
  END IF;

  CREATE OR REPLACE TRIGGER shop_changes_ins AFTER INSERT ON whatsapp.outbound_messages
    REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT
    EXECUTE FUNCTION business_app_core.bump_shop_changes('whatsapp');
  CREATE OR REPLACE TRIGGER shop_changes_upd AFTER UPDATE ON whatsapp.outbound_messages
    REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT
    EXECUTE FUNCTION business_app_core.bump_shop_changes('whatsapp');
  CREATE OR REPLACE TRIGGER shop_changes_del AFTER DELETE ON whatsapp.outbound_messages
    REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT
    EXECUTE FUNCTION business_app_core.bump_shop_changes('whatsapp');

  CREATE OR REPLACE TRIGGER shop_changes_ins AFTER INSERT ON whatsapp.inbound_messages
    REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT
    EXECUTE FUNCTION business_app_core.bump_shop_changes('whatsapp');
  CREATE OR REPLACE TRIGGER shop_changes_upd AFTER UPDATE ON whatsapp.inbound_messages
    REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT
    EXECUTE FUNCTION business_app_core.bump_shop_changes('whatsapp');
  CREATE OR REPLACE TRIGGER shop_changes_del AFTER DELETE ON whatsapp.inbound_messages
    REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT
    EXECUTE FUNCTION business_app_core.bump_shop_changes('whatsapp');
END
$$;
