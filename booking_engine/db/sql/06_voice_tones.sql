-- Migration 06: voice_agent.voice_tones + shop_config.tone_id
--
-- Replaces the fixed tone_preset enum (warm/professional/casual) with a
-- proper voice_tones table: 8 seeded presets today, with room for
-- shop-authored custom tones later (created_by_shop_id, globally unique name).
-- shop_config now references a tone by id instead of storing a preset
-- string inline.
--
-- Additive + idempotent — safe to re-run. The tone_preset -> tone_id switch
-- is a one-way move: no shop had a real tone_preset customization yet
-- (voice-agent config isn't live in production), so no backfill is needed.
--
-- Apply to Neon BEFORE deploying the code that reads/writes tone_id:
--   psql "$DATABASE_URL" -f booking_engine/db/sql/06_voice_tones.sql

CREATE TABLE IF NOT EXISTS voice_agent.voice_tones (
  id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name                       text NOT NULL UNIQUE,
  description                text NOT NULL,
  system_prompt_instruction  text NOT NULL,
  is_preset                  boolean NOT NULL DEFAULT true,
  created_by_shop_id         uuid REFERENCES business_app_core.shops(id) ON DELETE CASCADE,
  created_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS voice_tones_is_preset_idx
  ON voice_agent.voice_tones (is_preset);

ALTER TABLE voice_agent.shop_config
  ADD COLUMN IF NOT EXISTS tone_id uuid REFERENCES voice_agent.voice_tones(id);

ALTER TABLE voice_agent.shop_config
  DROP COLUMN IF EXISTS tone_preset;

INSERT INTO voice_agent.voice_tones (name, description, system_prompt_instruction, is_preset)
VALUES
  ('professionale', 'Formale, preciso, dà del lei',
   'Usa un tono professionale e formale. Dai sempre del lei al cliente. Sii preciso e conciso.', true),
  ('amichevole', 'Caloroso, informale, dà del tu',
   'Usa un tono amichevole e caloroso. Dai del tu al cliente. Sii accogliente e disponibile.', true),
  ('efficiente', 'Diretto, va dritto al punto',
   'Vai dritto al punto. Evita convenevoli superflui. Rispondi in modo rapido e diretto.', true),
  ('luxury', 'Elegante, curato, esperienza premium',
   'Usa un linguaggio elegante e ricercato, coerente con un''esperienza premium. Dai del lei.', true),
  ('tecnico', 'Preciso sui dettagli di servizi e trattamenti',
   'Sii preciso e dettagliato quando descrivi servizi e trattamenti. Usa terminologia di settore corretta.', true),
  ('casual', 'Rilassato, colloquiale',
   'Usa un tono rilassato e colloquiale, come una chiacchierata tra amici. Dai del tu.', true),
  ('empatico', 'Attento, mostra comprensione verso le esigenze del cliente',
   'Mostra attenzione ed empatia verso le esigenze del cliente. Ascolta e rassicura prima di procedere.', true),
  ('conciso', 'Risposte brevi, minimo indispensabile',
   'Rispondi nel modo più breve possibile. Comunica solo le informazioni essenziali.', true)
ON CONFLICT (name) DO NOTHING;
