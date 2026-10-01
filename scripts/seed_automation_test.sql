-- Seed one run of the WhatsApp automation rules on the QA demo shop.
--
-- The automations feature is fully implemented; this only creates the rows the
-- hourly tick needs to find "due work", so a single POST /api/v1/messaging/tick
-- fires a real template send. Idempotent (fixed UUIDs + ON CONFLICT); touches
-- only the demo shop and only rows it owns.
--
-- QA BRANCH ONLY. Never run against production (host fragment
-- ep-weathered-term-agsfwl6w): an in-transaction guard refuses that branch by
-- the connected host's name, the caller must pass -v confirm_qa=yes, and a
-- missing confirm_qa, a confirm_qa other than "yes", or a missing recipient
-- makes the guards below refuse and exit non-zero.
--
-- Scope warning: the tick's `run_automations` stage processes EVERY due
-- appointment for the shop, not just the two seeded rows below. Once the rules
-- are enabled, any other due appointment in this shop is messaged on the next
-- tick too; the visibility query near the end lists those rows so the wider
-- blast radius is a known quantity before the tick runs.
--
-- Usage:
--   psql "$QA_DATABASE_URL" -v confirm_qa=yes \
--     -v recipient='+39XXXXXXXXXX' -f scripts/seed_automation_test.sql
--   (# -v shop='...' overrides the default demo shop)
--
-- Then: POST /api/v1/messaging/tick, wait for the 60s drain, read
-- whatsapp.outbound_messages WHERE campaign_key LIKE 'automation:%'.
-- Clean up with scripts/cleanup_automation_test.sql.

-- ON_ERROR_STOP first, so a refusal below aborts psql with a non-zero exit.
\set ON_ERROR_STOP on

\if :{?confirm_qa}
SELECT CASE WHEN :'confirm_qa' = 'yes' THEN 1 ELSE 1/0 END AS confirmed;
\else
\echo 'Refusing to run: pass -v confirm_qa=yes (QA branch only).'
SELECT 1/0 AS refused;
\endif

\if :{?shop}
\else
\set shop 5e0b3ecf-c85f-478f-9369-859c419e7df0
\endif

\if :{?recipient}
\else
\echo 'Refusing to run: pass -v recipient=''+39XXXXXXXXXX'' (an allowlisted test recipient).'
SELECT 1/0 AS refused;
\endif

BEGIN;

-- Guards. No DO block: psql does not interpolate :vars inside dollar-quotes.
-- 1/0 forces a hard error under ON_ERROR_STOP when the input is unusable.
SELECT CASE WHEN coalesce(trim(:'recipient'), '') = ''
            THEN 1/0 ELSE 1 END AS recipient_is_set;
-- count(*) is non-constant, so 1/0 is not constant-folded at plan time: it
-- raises only when the shop is absent and returns 1 when present.
SELECT 1 / count(*) AS shop_exists
FROM business_app_core.shops WHERE id = :'shop'::uuid;

-- Refuse the known production branch. psql's automatic :HOST variable holds the
-- connected server host (empty when connected over a local socket).
SELECT CASE WHEN :'HOST' LIKE '%ep-weathered-term-agsfwl6w%'
            THEN 1/0 ELSE 1 END AS host_not_prod;

-- What the tick's gates will see: the sender must be online, and both
-- templates must be approved (feedback_v2 as MARKETING, reminder_v6 as UTILITY).
SELECT 'sender' AS what, status, quality_rating, phone_number, phone_number_id
FROM whatsapp.senders WHERE shop_id = :'shop'::uuid;
SELECT 'template:' || template_key AS what, status, category, name, language
FROM whatsapp.templates
WHERE shop_id = :'shop'::uuid AND template_key IN ('feedback_v2','reminder_v6')
ORDER BY template_key;

-- 1) enable both rules
INSERT INTO whatsapp.automation_rules (shop_id, rule_key, enabled, params)
VALUES (:'shop'::uuid, 'feedback', true,
        '{"hours_after":24,"platform":"general","link":""}'::jsonb),
       (:'shop'::uuid, 'reminder', true,
        '{"min_no_shows":0}'::jsonb)
ON CONFLICT (shop_id, rule_key) DO UPDATE
SET enabled = EXCLUDED.enabled,
    params  = EXCLUDED.params,
    updated_at = now();

-- 2) test rows, fixed UUIDs (cleanup depends on them)
INSERT INTO business_app_core.staff (id, shop_id, full_name)
VALUES ('a0000000-0000-4000-8000-000000000001', :'shop'::uuid, 'Stylist Test')
ON CONFLICT (id) DO NOTHING;

INSERT INTO business_app_core.services
  (id, shop_id, service_name, duration_minutes, category)
VALUES ('a0000000-0000-4000-8000-000000000002', :'shop'::uuid, 'Taglio Test', 30, 'taglio')
ON CONFLICT (id) DO NOTHING;

-- Recipient with active marketing consent (only the MARKETING feedback rule
-- needs it; the UTILITY reminder ignores consent and cooldown).
INSERT INTO business_app_core.customers
  (id, shop_id, full_name, phone, marketing_consent,
   marketing_consent_granted_at, marketing_consent_withdrawn_at)
VALUES ('a0000000-0000-4000-8000-000000000003', :'shop'::uuid, 'Maria Test',
        :'recipient', true, now(), NULL)
ON CONFLICT (id) DO UPDATE
SET phone = EXCLUDED.phone,
    marketing_consent = true,
    marketing_consent_granted_at = now(),
    marketing_consent_withdrawn_at = NULL;

-- 3) due work: reminder starts within 24h; feedback completed ~24h30m ago
--    (the due window is now-25h .. now-24h, so 30m sits mid-window).
INSERT INTO business_app_core.appointments
  (id, shop_id, customer_id, staff_id, start_time, end_time, status)
VALUES ('a0000000-0000-4000-8000-000000000004', :'shop'::uuid,
        'a0000000-0000-4000-8000-000000000003',
        'a0000000-0000-4000-8000-000000000001',
        now() + interval '2 hours', now() + interval '2 hours 30 minutes',
        'scheduled')
ON CONFLICT (id) DO UPDATE
SET start_time = EXCLUDED.start_time,
    end_time   = EXCLUDED.end_time,
    status     = EXCLUDED.status;

INSERT INTO business_app_core.appointments
  (id, shop_id, customer_id, staff_id, start_time, end_time, status)
VALUES ('a0000000-0000-4000-8000-000000000005', :'shop'::uuid,
        'a0000000-0000-4000-8000-000000000003',
        'a0000000-0000-4000-8000-000000000001',
        now() - interval '24 hours 30 minutes' - interval '70 minutes',
        now() - interval '24 hours 30 minutes',
        'completed')
ON CONFLICT (id) DO UPDATE
SET start_time = EXCLUDED.start_time,
    end_time   = EXCLUDED.end_time,
    status     = EXCLUDED.status;

INSERT INTO business_app_core.appointment_services (appointment_id, service_id, duration_minutes)
VALUES ('a0000000-0000-4000-8000-000000000004','a0000000-0000-4000-8000-000000000002', 30),
       ('a0000000-0000-4000-8000-000000000005','a0000000-0000-4000-8000-000000000002', 30)
ON CONFLICT DO NOTHING;

-- 4) make both appointments "due" again if a previous run already logged them.
--    The enqueue is ON CONFLICT (shop_id, campaign_key, customer_id) DO NOTHING,
--    so the prior outbound rows must go too or a re-run never re-enqueues.
DELETE FROM whatsapp.outbound_messages
WHERE campaign_key IN (
  'automation:reminder:a0000000-0000-4000-8000-000000000004',
  'automation:feedback:a0000000-0000-4000-8000-000000000005'
);

DELETE FROM whatsapp.automation_sends
WHERE appointment_id IN ('a0000000-0000-4000-8000-000000000004',
                         'a0000000-0000-4000-8000-000000000005');

-- Readiness (mirrors the two due-work queries): both should report due = true.
SELECT 'reminder' AS rule, a.id, a.status,
       (a.status IN ('scheduled','confirmed')
        AND a.start_time > now() AND a.start_time <= now() + interval '24 hours'
        AND coalesce(c.phone,'') <> ''
        AND NOT EXISTS (SELECT 1 FROM whatsapp.automation_sends s
                        WHERE s.rule_key='reminder' AND s.appointment_id=a.id)) AS due
FROM business_app_core.appointments a
JOIN business_app_core.customers c ON c.id = a.customer_id
WHERE a.id = 'a0000000-0000-4000-8000-000000000004'
UNION ALL
SELECT 'feedback', a.id, a.status,
       (a.status='completed'
        AND a.end_time > now() - interval '25 hours'
        AND a.end_time <= now() - interval '24 hours'
        AND coalesce(c.phone,'') <> ''
        AND NOT EXISTS (SELECT 1 FROM whatsapp.automation_sends s
                        WHERE s.rule_key='feedback' AND s.appointment_id=a.id)) AS due
FROM business_app_core.appointments a
JOIN business_app_core.customers c ON c.id = a.customer_id
WHERE a.id = 'a0000000-0000-4000-8000-000000000005';

-- WARNING: the tick processes the whole shop, not just the rows above. These
-- rows (other than the seeded ones) have not already been sent and will be
-- picked up next tick, subject to the rule's own filters (e.g. reminder's
-- min_no_shows, which the seed sets to 0 so that no-show filter is inert
-- here). This is a blast-radius aid, not itself a due query.
SELECT 'other_due_reminder' AS kind, a.id, a.start_time, c.phone
FROM business_app_core.appointments a
JOIN business_app_core.customers c ON c.id = a.customer_id
WHERE a.shop_id = :'shop'::uuid
  AND a.status IN ('scheduled','confirmed')
  AND a.start_time > now() AND a.start_time <= now() + interval '24 hours'
  AND coalesce(c.phone,'') <> ''
  AND NOT EXISTS (SELECT 1 FROM whatsapp.automation_sends s
                  WHERE s.rule_key='reminder' AND s.appointment_id=a.id)
  AND a.id <> 'a0000000-0000-4000-8000-000000000004'
UNION ALL
SELECT 'other_due_feedback', a.id, a.end_time, c.phone
FROM business_app_core.appointments a
JOIN business_app_core.customers c ON c.id = a.customer_id
WHERE a.shop_id = :'shop'::uuid
  AND a.status = 'completed'
  AND a.end_time > now() - interval '25 hours'
  AND a.end_time <= now() - interval '24 hours'
  AND coalesce(c.phone,'') <> ''
  AND NOT EXISTS (SELECT 1 FROM whatsapp.automation_sends s
                  WHERE s.rule_key='feedback' AND s.appointment_id=a.id)
  AND a.id <> 'a0000000-0000-4000-8000-000000000005';

COMMIT;
