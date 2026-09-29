-- Remove everything scripts/seed_automation_test.sql created, restoring the
-- pre-test "no automations" state by default. Fixed UUIDs and campaign keys
-- only, so no real demo-shop data is touched. The seeded rules of an
-- overridden shop are removed too: pass the same -v shop='...' the seed used.
--
-- QA BRANCH ONLY. Never run against production (host fragment
-- ep-weathered-term-agsfwl6w): a guard refuses that branch by the connected
-- host's name, the caller must pass -v confirm_qa=yes, and a confirm_qa other
-- than "yes" makes the guard refuse and exit non-zero.
--
-- Usage: psql "$QA_DATABASE_URL" -v confirm_qa=yes \
--          -f scripts/cleanup_automation_test.sql
--        (# -v shop='...' overrides the default demo shop, mirroring the seed)

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

-- Refuse the known production branch. psql's automatic :HOST variable holds the
-- connected server host (empty when connected over a local socket).
SELECT CASE WHEN :'HOST' LIKE '%ep-weathered-term-agsfwl6w%'
            THEN 1/0 ELSE 1 END AS host_not_prod;

BEGIN;

DELETE FROM whatsapp.outbound_messages
WHERE campaign_key IN (
  'automation:reminder:a0000000-0000-4000-8000-000000000004',
  'automation:feedback:a0000000-0000-4000-8000-000000000005'
);

DELETE FROM whatsapp.automation_sends
WHERE appointment_id IN ('a0000000-0000-4000-8000-000000000004',
                         'a0000000-0000-4000-8000-000000000005');

DELETE FROM business_app_core.appointment_services
WHERE appointment_id IN ('a0000000-0000-4000-8000-000000000004',
                         'a0000000-0000-4000-8000-000000000005');

DELETE FROM business_app_core.appointments
WHERE id IN ('a0000000-0000-4000-8000-000000000004',
             'a0000000-0000-4000-8000-000000000005');

DELETE FROM business_app_core.customers WHERE id = 'a0000000-0000-4000-8000-000000000003';
DELETE FROM business_app_core.services  WHERE id = 'a0000000-0000-4000-8000-000000000002';
DELETE FROM business_app_core.staff     WHERE id = 'a0000000-0000-4000-8000-000000000001';

-- Turn the rules off, restoring the pre-test "no automations" state.
DELETE FROM whatsapp.automation_rules
WHERE shop_id = :'shop'::uuid
  AND rule_key IN ('feedback','reminder');

-- To leave the rules enabled for further testing, comment out the DELETE above.

COMMIT;
