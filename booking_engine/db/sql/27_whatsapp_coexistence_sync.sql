-- Coexistence sync: Meta offboards a coexistence number unless the partner
-- calls POST /{phone_number_id}/smb_app_data within 24h of onboarding —
-- contacts (`smb_app_state_sync`) first, then `history`. Each can be called
-- exactly once per onboarding, so each step records its own success: a retry
-- after a half-done sync must skip the step that already went through.
-- Confirmed by Meta developer support 2026-09-25.
--
-- NULL = not requested yet, the truth for every existing row. Idempotent.

ALTER TABLE whatsapp.senders
  ADD COLUMN IF NOT EXISTS contacts_sync_at timestamptz,
  ADD COLUMN IF NOT EXISTS history_sync_at  timestamptz;
