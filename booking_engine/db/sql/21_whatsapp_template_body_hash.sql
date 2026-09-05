-- Which VERSION of the copy a salon's WABA actually holds.
--
-- `status = 'approved'` only ever meant "a template with this name passed
-- review on this WABA". It said nothing about which body — so re-voicing a
-- template in whatsapp_templates.py changed nothing downstream: ensure_templates
-- skipped every key it already had a row for, and every connected salon kept
-- sending the old text with the status column reading `approved`.
--
-- NULL on every existing row, deliberately, and NOT backfilled: NULL means
-- "unknown version", which is exactly what it is, and it makes the first sweep
-- after this migration edit each template once to the catalogue's current body
-- (gated, as always, on Kairo's own WABA having approved that same body first).
-- Backfilling to the current hash would instead assert an alignment nobody has
-- checked against Meta.
ALTER TABLE whatsapp.templates ADD COLUMN IF NOT EXISTS body_hash text;

COMMENT ON COLUMN whatsapp.templates.body_hash IS
  'sha256(body)[:32] of the copy last pushed to this WABA. NULL = unknown, '
  'treated as stale. See whatsapp_templates.body_hash().';
