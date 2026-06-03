-- 04_voice_agent_v2.sql
-- Additive migration to support live voice booking, identity resolution,
-- callback memos, telephony provisioning, and graceful token detach.
-- All changes are additive: no existing column is removed or retyped.

BEGIN;

-- 1. business_app_core.customers — additive
ALTER TABLE business_app_core.customers
ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual'
CHECK (source IN ('manual','voice_agent','import','whatsapp')),
ADD COLUMN IF NOT EXISTS created_by_call_id uuid,
ADD COLUMN IF NOT EXISTS verified boolean NOT NULL DEFAULT true,
ADD COLUMN IF NOT EXISTS phone_verified boolean NOT NULL DEFAULT true,
ADD COLUMN IF NOT EXISTS phone_normalized text
GENERATED ALWAYS AS (regexp_replace(coalesce(phone,''),'\D','','g')) STORED,
ADD COLUMN IF NOT EXISTS household_of uuid REFERENCES business_app_core.customers(id),
ADD COLUMN IF NOT EXISTS phone_shared_with uuid REFERENCES business_app_core.customers(id);

CREATE INDEX IF NOT EXISTS customers_shop_phone_normalized_idx
ON business_app_core.customers(shop_id, phone_normalized)
WHERE phone_normalized != '';

-- 2. business_app_core.appointments — additive
ALTER TABLE business_app_core.appointments
ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual'
CHECK (source IN ('manual','voice_agent','whatsapp')),
ADD COLUMN IF NOT EXISTS voice_call_id uuid,
ADD COLUMN IF NOT EXISTS confirmation_status text NOT NULL DEFAULT 'confirmed'
CHECK (confirmation_status IN ('confirmed','pending_sms_confirmation','verification_failed'));

-- 3. voice_agent.shop_telephony
CREATE TABLE IF NOT EXISTS voice_agent.shop_telephony (
shop_id uuid PRIMARY KEY REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
provider text NOT NULL DEFAULT 'twilio',
kairo_number text NOT NULL,
kairo_number_sid text NOT NULL,
salon_existing_number text,
salon_existing_normalized text GENERATED ALWAYS AS
(regexp_replace(coalesce(salon_existing_number,''),'\D','','g')) STORED,
setup_path text NOT NULL CHECK (setup_path IN ('new','forward')),
provisioned_at timestamptz NOT NULL DEFAULT now(),
last_inbound_call_at timestamptz
);

-- 4. voice_agent.shop_config — extend if exists, create if missing
CREATE TABLE IF NOT EXISTS voice_agent.shop_config (
shop_id uuid PRIMARY KEY REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
created_at timestamptz NOT NULL DEFAULT now(),
updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE voice_agent.shop_config
ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT false,
ADD COLUMN IF NOT EXISTS display_name text NOT NULL DEFAULT '',
ADD COLUMN IF NOT EXISTS greeting_after_disclosure text NOT NULL DEFAULT '',
ADD COLUMN IF NOT EXISTS voice_preset text NOT NULL DEFAULT 'warm_female',
ADD COLUMN IF NOT EXISTS tone_preset text NOT NULL DEFAULT 'warm',
ADD COLUMN IF NOT EXISTS business_hours jsonb NOT NULL DEFAULT '{}'::jsonb,
ADD COLUMN IF NOT EXISTS answer_mode text NOT NULL DEFAULT 'overflow'
CHECK (answer_mode IN ('overflow','always_on')),
ADD COLUMN IF NOT EXISTS overflow_ring_count smallint NOT NULL DEFAULT 4,
ADD COLUMN IF NOT EXISTS services_to_mention uuid[] NOT NULL DEFAULT '{}'::uuid[],
ADD COLUMN IF NOT EXISTS retention_days smallint NOT NULL DEFAULT 90,
ADD COLUMN IF NOT EXISTS manual_fallback_number text,
ADD COLUMN IF NOT EXISTS manual_fallback_normalized text
GENERATED ALWAYS AS (regexp_replace(coalesce(manual_fallback_number,''),'\D','','g')) STORED,
ADD COLUMN IF NOT EXISTS auto_topup_enabled boolean NOT NULL DEFAULT false,
ADD COLUMN IF NOT EXISTS auto_topup_threshold_tokens integer,
ADD COLUMN IF NOT EXISTS auto_topup_package_id uuid;

-- 5. voice_agent.callback_memos
CREATE TABLE IF NOT EXISTS voice_agent.callback_memos (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
call_id uuid NOT NULL REFERENCES voice_agent.calls(id) ON DELETE CASCADE,
shop_id uuid NOT NULL REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
customer_id uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
caller_phone text,
reason text NOT NULL,
callback_window text,
status text NOT NULL DEFAULT 'pending'
CHECK (status IN ('pending','actioned','dismissed')),
actioned_by uuid,
actioned_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS callback_memos_shop_status_idx
ON voice_agent.callback_memos(shop_id, status, created_at DESC);

-- 6. voice_agent.auth_events
CREATE TABLE IF NOT EXISTS voice_agent.auth_events (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
call_id uuid NOT NULL REFERENCES voice_agent.calls(id) ON DELETE CASCADE,
customer_id uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
verification_question text NOT NULL,
caller_answer_excerpt text,
passed boolean NOT NULL,
created_at timestamptz NOT NULL DEFAULT now()
);

-- 7. voice_agent.system_policy
CREATE TABLE IF NOT EXISTS voice_agent.system_policy (
locale text PRIMARY KEY,
disclosure_text text NOT NULL,
recording_consent_prompt text NOT NULL,
policy_version integer NOT NULL,
effective_from date NOT NULL DEFAULT current_date
);

INSERT INTO voice_agent.system_policy (locale, disclosure_text, recording_consent_prompt, policy_version)
VALUES (
'it-IT',
'Salve, questa chiamata è gestita da un assistente vocale automatico. La conversazione verrà trascritta per finalità di servizio. Continuando la chiamata acconsente al trattamento.',
'Posso aiutarla con la sua prenotazione?',
1
)
ON CONFLICT (locale) DO NOTHING;

-- 8. voice_agent.calls — extend with shop_id and identity links
ALTER TABLE voice_agent.calls
ADD COLUMN IF NOT EXISTS shop_id uuid REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
ADD COLUMN IF NOT EXISTS matched_customer_id uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS created_customer_id uuid REFERENCES business_app_core.customers(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS created_booking_id uuid REFERENCES business_app_core.appointments(id) ON DELETE SET NULL;

-- 9. business_app_core.ai_token_basket_events — additive
ALTER TABLE business_app_core.ai_token_basket_events
ADD COLUMN IF NOT EXISTS voice_call_id uuid REFERENCES voice_agent.calls(id) ON DELETE SET NULL;

-- Allow 'voice_call' as a source value (CHECK constraint relaxation if present)
DO $$
BEGIN
IF EXISTS (
SELECT 1 FROM pg_constraint
WHERE conname = 'ai_token_basket_events_source_check'
) THEN
ALTER TABLE business_app_core.ai_token_basket_events
DROP CONSTRAINT ai_token_basket_events_source_check;
END IF;
ALTER TABLE business_app_core.ai_token_basket_events
ADD CONSTRAINT ai_token_basket_events_source_check
CHECK (source IN ('chat','voice_call','manual','system'));
END$$;

COMMIT;
