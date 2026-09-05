-- The salon's business token can expire, and migration 15 said it couldn't.
--
-- 15's comment on `access_token` read "It does not expire unless the salon
-- revokes our app". That was an assumption about Embedded Signup, never a
-- checked fact — and the app's actual Login Configuration
-- (`Configurazione dell'iscrizione integrata di WhatsApp con token con
-- scadenza a 60 giorni`) mints tokens that expire in 60 days. Left as-is,
-- every connected salon would stop sending 60 days after onboarding, all at
-- once, with nothing anywhere saying why: the token is the *only* credential
-- for that WABA — there is no shared parent to fall back to.
--
-- Nothing renews it. A business token comes into existence by the salon
-- completing the popup, so recovery is asking them to reconnect. This column
-- exists so that conversation can start before the sends fail, not after.
--
-- NULL means Meta reported no `expires_in` — either the config was changed to
-- issue non-expiring tokens, or the sender predates this migration. Existing
-- rows are deliberately not backfilled: "unknown" is the truth for them, and
-- inventing `onboarded_at + 60 days` would assert an expiry nobody read back
-- from Meta. Same reasoning as migration 21's NULL `body_hash`.
--
-- Idempotent: migrate.sh re-applies every file.

ALTER TABLE whatsapp.senders ADD COLUMN IF NOT EXISTS token_expires_at timestamptz;

COMMENT ON COLUMN whatsapp.senders.token_expires_at IS
  'When the Embedded Signup business token stops working. NULL = Meta '
  'reported no expiry, or the sender predates migration 22. Nothing renews '
  'it; recovery is the salon redoing Embedded Signup.';

COMMENT ON COLUMN whatsapp.senders.access_token IS
  'Customer-scoped business token from Embedded Signup''s code exchange. The '
  'only credential for this WABA — there is no shared parent. May expire; see '
  'token_expires_at. Stored in plaintext (flagged, not fixed).';
