-- BYO WABA only: the 'new' onboarding path (a fresh WABA provisioned through
-- us, on a number not yet on WhatsApp) is removed. See CLAUDE.md §2026-08-30.
-- Every sender created before this migration is already 'coexistence' — 'new'
-- was flagged in the same CLAUDE.md entry that removed it as never having
-- been exercised against a real WABA, so this UPDATE is a no-op in practice,
-- kept for safety rather than assumed. Idempotent: migrate.sh re-applies
-- every file.

ALTER TABLE whatsapp.senders DROP CONSTRAINT IF EXISTS senders_source_check;
UPDATE whatsapp.senders SET source = 'coexistence' WHERE source <> 'coexistence';
ALTER TABLE whatsapp.senders ADD CONSTRAINT senders_source_check
  CHECK (source = 'coexistence');
