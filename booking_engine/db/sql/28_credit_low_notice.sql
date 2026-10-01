-- The low-credit email: once per episode, not once per hour.
--
-- Owner decision, 2026-09-29: below the shop's low-credit threshold
-- (`auto_topup_threshold_tokens`, 10 000 when NULL — services/credit_state.py)
-- the WhatsApp responder pauses and every conversation becomes the owner's.
-- The cockpit banner says so, but it is pull-only; the email is what reaches
-- an owner who has not opened the app. The hourly tick would otherwise send it
-- every hour for as long as the basket stays low.
--
-- Set when the tick asks the webapp to send (the attempt, not the delivery —
-- same rule as `whatsapp.senders.token_reminder_sent_at`, migration 23), and
-- cleared by the first tick that finds the basket back above the threshold,
-- so the *next* episode sends again. NULL = no episode in progress, the truth
-- for every existing row.
--
-- Lives on shop_config beside the threshold it is about. Idempotent:
-- migrate.sh re-applies every file.

ALTER TABLE voice_agent.shop_config
  ADD COLUMN IF NOT EXISTS credit_low_notified_at timestamptz;

COMMENT ON COLUMN voice_agent.shop_config.credit_low_notified_at IS
  'When the tick asked the webapp to email the owner that the basket fell to '
  'the low-credit threshold and the WhatsApp responder paused. Cleared when '
  'the balance is back above it, so each episode mails once. Records the '
  'attempt, not the delivery. NULL = not in a low-credit episode.';
