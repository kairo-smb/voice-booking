# Database

> **Maintenance rule:** a schema migration that adds/removes/renames a `voice_agent` table or column updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Ownership boundary

Three schemas in one Neon Postgres database:

| Schema | Owner | This repo's access |
|---|---|---|
| `business_app_core` | the `webapp` Control Plane repo | reads/writes narrowly through `booking_engine/db/queries.py`; **never alters its DDL** |
| `voice_agent` | this repo | owns it fully — DDL lives in `booking_engine/db/sql/`, applied in order by `scripts/migrate.sh` |
| `sms` | this repo | owns it fully, added 2026-08-12 — see [`sms` schema](#sms-schema--authoritative-here) below |

**Do not hand-copy `business_app_core`'s schema into a doc.** That has already gone stale and caused real bugs at least twice (`CLAUDE.md` §2026-07-24 "Repo cleanup..." and the schema-mismatch history it references). The accurate, current mapping is `booking_engine/db/queries.py`, exercised against real Neon-shaped data by `tests/live_db/*`. Read that file for column names, not this one.

### `business_app_core` write contract, by table

This isn't schema (column names) — it's the write-boundary contract, which is far more stable and doesn't share the drift risk above. Verified directly against `booking_engine/db/queries.py`'s actual queries, not carried over from an old doc:

| Table | This repo's access |
|---|---|
| `shops`, `staff`, `services` | Read-only, filtered `is_active = true` |
| `staff_services`, `staff_schedules` | Read-only (junction/schedule reads, no `is_active` column of their own — the join partner's `is_active` filter is what gates them) |
| `customers` | Read + Create |
| `phone_contacts` | Read + Create/Upsert |
| `appointments` | Read + Create + Cancel (reschedule = cancel-old + create-new, two separate statements — see the race note below) |
| `appointment_services` | Read + Create (via appointment) |

The Control Plane (`webapp`) has full CRUD on all of the above except `appointments`, where it additionally owns every status transition this repo doesn't make (see below) — full detail on Control Plane's own side lives in that repo, not here.

**`appointments.status` lifecycle:** `scheduled → confirmed → completed`, with `cancelled`/`no_show` reachable from `scheduled`/`confirmed` (confirmed via `queries.py`'s `status IN ('scheduled', 'confirmed')` checks before cancel, and `status NOT IN ('cancelled', 'no_show')` when computing availability). **This repo only ever writes `scheduled` (on create) or `cancelled` (on cancel/reschedule)** — `confirmed`, `completed`, and `no_show` are Control Plane-only transitions.

**The overlap check before an insert is a plain check-then-insert, not a transaction — a known, accepted race.** `create_appointment`/`create_appointment_chain` in `queries.py` run a `SELECT` for conflicting appointments (`staff_id`, `status NOT IN ('cancelled', 'no_show')`, overlapping time range) and then a separate `INSERT`, each its own `pool.acquire()` (`db/connection.py::execute`/`execute_void` don't share a connection or transaction across calls). `voice_tool_queries.py` says so explicitly: `ponytail: relies on the create_appointment*/_chain overlap check (no advisory lock). Add one only if concurrent voice bookings for the same staff+slot become a real problem.` Any new write path into `appointments` should at minimum replicate the same check-then-insert (to catch the common case), and should add real locking if it can't accept this race.

**IDs are generated in Python** (`uuid4()`, `booking_engine/db/queries.py`), passed explicitly into every INSERT — not `gen_random_uuid()` at the database level (that convention is what the *bootstrap-only* `01_schema.sql` uses for its own tables, and does not describe how this repo's code actually writes to real `business_app_core`).

**Other stable conventions:** soft deletes via `is_active = false` (never hard-delete a row with dependent appointments); timezone is hardcoded `Europe/Rome` for all slot calculations (`ZoneInfo("Europe/Rome")` in `queries.py`).

**`customers.marketing_consent*` columns appear in no migration in this repo.** They arrive via the webapp's own migration chain, not this repo's — noted here so nobody re-diagnoses that as a missing-migration bug (found while grounding the 2026-08-14 number-provisioning docs; see `CLAUDE.md`).

## `voice_agent` schema — authoritative here

DDL: `booking_engine/db/sql/03_voice_agent_schema.sql` through `13_number_release.sql`, applied in filename order. `01_schema.sql`/`02_seed_data.sql` are a **separate, local-only bootstrap pair** with fake data and unqualified table names — `scripts/migrate.sh` explicitly skips both; never run them against real Neon.

| Table | Added in | Purpose |
|---|---|---|
| `calls` | 03, extended 04/08 | one row per inbound call — caller number, matched/created customer, outcome, and (08) a structured hairstylist `service_brief` |
| `call_transcripts` | 03 | per-turn transcript rows for a call |
| `call_events` | 03 | tool-call/event log for a call |
| `shop_telephony` | 04, extended 09/12/13 | provisioned Twilio number per shop, `setup_path` (new/forward), `provider` (defaults `'twilio'` since 09), `health_status`/`health_detail`/`health_checked_at` (12, green/red semaphore — see below), `release_scheduled_at` (13, grace-period release deadline — see below) |
| `shop_config` | 04, extended 06/07/10 | Layer 1 voice config: `enabled`, `display_name`, greetings, `voice_preset`, `tone_id` (06, FK to `voice_tones`, replaced an inline `tone_preset` string), `business_hours`, `answer_mode`, token top-up settings |
| `callback_memos` | 04 | merchant callback reminders created by `escalate_to_merchant` |
| `auth_events` | 04 | identity-verification audit trail |
| `system_policy` | 04 | disclosure/consent text (seeded it-IT) |
| `voice_tones` | 06 | 8 seeded presets (`is_preset=true`) plus room for shop-authored custom tones (`created_by_shop_id`); seeded names: professionale, amichevole, efficiente, luxury, tecnico, casual, empatico, conciso |
| `number_requests` | 12, extended 13 | one row per shop, PK `shop_id`: self-service Estonian-number regulatory-bundle lifecycle (`status` draft→evaluating→pending_review→approved/rejected→provisioned→**released** (13), the Twilio `regulation_sid`/`bundle_sid`/`end_user_sid`/`document_sid`, `evaluation_errors` jsonb verbatim from Twilio, `rejection_reason`, `released_at`/`released_number` (13, kept for history — see below)). Polled hourly by `POST /api/v1/messaging/tick`. See [Architecture](architecture.md#self-service-number-provisioning-path-2-onboarding) and `CLAUDE.md` §2026-08-14. |

**`shop_telephony`'s health semaphore (12):** `health_status` (`unknown`/`green`/`red`, default `unknown`) records whether a provisioned number still exists at Twilio with its voice webhook pointed at us. A Twilio-unreachable probe deliberately leaves the prior status untouched rather than flipping to red — only a confirmed 404 or voice-webhook drift changes the light (`services/number_health.py::decide_health`). `sms_url` is deliberately not checked — there is no inbound SMS handler any more (STOP handling removed; see `CLAUDE.md`), so there is nothing for it to correctly point at.

**Grace-period number release (13), closes the cancellation gap flagged in `CLAUDE.md` §2026-08-14.** When a shop's plan lapses (`shops.plan_id` goes `NULL`), the hourly tick doesn't release the number immediately — it stamps `shop_telephony.release_scheduled_at = now() + 14 days` the first time it notices, clears it if the plan comes back before that deadline, and only calls Twilio to release the number once the deadline has passed (`services/number_release.py::decide_release`/`sweep`). The deadline lives here, in `voice_agent`, derived from when *we* first observed the lapse — deliberately not a `plan_lapsed_at` column on `business_app_core.shops`, which is the webapp repo's schema. On release, `shop_telephony`'s row is deleted (Twilio confirms first, row deletion second — a lost Twilio call must not delete a row we're still paying for) and `number_requests.status` moves to `released` with `released_at`/`released_number` stamped so the history survives after the row is gone.

`business_app_core.shops` also gained two columns directly in migration 03: `voice` (default `'alloy'`) and `language` (default `'it'`) — the one place this repo's migrations touch the other schema, both additive/nullable-safe.

## `sms` schema — authoritative here

Added 2026-08-12 (`booking_engine/db/sql/11_sms_schema.sql`), owned by this
repo like `voice_agent`. Phase 1 of a larger SMS/WhatsApp messaging design —
see [Architecture → SMS marketing send](architecture.md#sms-marketing-send-phase-1-of-messaging)
and `CLAUDE.md` §2026-08-12. WhatsApp now has its own schema — see
[`whatsapp` schema](#whatsapp-schema--authoritative-here) below.

| Table | Purpose |
|---|---|
| `campaigns` | batch-send container (`draft → approved → sending → sent/cancelled`). **Exists, nothing writes to it yet** — Phase 1 is one-off sends only. |
| `outbound_messages` | one row per send attempt, including refused ones (`status='suppressed'`, `suppressed_reason` — a refusal is always persisted, never silently dropped). `credits_charged`/`price_usd` are the billed figures; `campaign_id IS NULL` means a one-off send. Unique on `(campaign_id, customer_id)` where both are set, so re-running a batch send can't double-message a customer. |
| `opt_outs` | **unused as of the STOP-removal (see `CLAUDE.md`).** Kept in the schema, no `DROP TABLE` — intentionally left behind rather than dropped — but nothing reads or writes it any more. |

**STOP handling removed; suppression is `customers.marketing_consent`
alone.** This repo previously reimplemented STOP-keyword parsing in
application code (Twilio's automatic STOP handling doesn't cover the
Estonian DID) and wrote both `sms.opt_outs` and
`customers.marketing_consent = false` on a recognised STOP reply. The owner
decided to remove that entirely — opt-out is now handled in-store, by a
staff member clearing marketing consent in the app. There is no inbound SMS
webhook and no opt-out footer any more; `sms_send.py`'s only suppression
check is `customers.marketing_consent`/`_granted_at`/`_withdrawn_at`. See
`CLAUDE.md`'s STOP-removal entry for the full reasoning, including the
explicit note that this is a weaker position under Italian marketing rules,
accepted as the owner's decision.

**Gap worth knowing:** `business_app_core.customers.marketing_consent`,
`_granted_at`, `_withdrawn_at`, `_source` exist on the live database but
appear in **no migration file inside this repo** — they were added through
the webapp's own migration chain, not this repo's. Grepping only this repo
for those columns will come up empty; they're real, just owned elsewhere —
same ownership-boundary caution as the rest of `business_app_core` above.

`business_app_core.ai_token_log` gained two nullable, FK-less columns in
the same change — `sms_message_id`, `whatsapp_message_id` — via the
**webapp's** own migration (`46_ai_token_log_message_fk.sql`), not this
repo's chain, since `ai_token_log` lives in `business_app_core`. They
mirror the existing `voice_call_id` column so a credit debit for a message
send is traceable back to the row that caused it; no FK, since
`sms.outbound_messages`/future `whatsapp.messages` are owned by this repo
and `ai_token_log` must not depend on a schema it doesn't own.

## `whatsapp` schema — authoritative here

Added 2026-08-21 (`14_whatsapp_schema.sql`), reshaped for Meta Cloud API on
2026-08-24 (`15_whatsapp_meta.sql`). Owned by this repo. See
[Architecture → WhatsApp marketing](architecture.md#whatsapp-marketing-one-waba-per-salon),
[Providers → WhatsApp](providers.md#whatsapp-meta-cloud-api-tech-provider),
[API → WhatsApp](api/whatsapp.md), and `CLAUDE.md` §2026-08-24.

| Table | Purpose |
|---|---|
| `senders` | one row per shop: the salon's `waba_id`, `phone_number_id`, the customer-scoped `access_token` from Embedded Signup, `platform_type` (`COEXISTENCE` when the number is also live on the WhatsApp Business App), display name, `quality_rating`, and **two different Meta ceilings**: `messaging_limit` (the volume tier — conversations per *rolling* 24h) and `throughput_level` (messages per second). `daily_cap` is Kairo's own drip rate and only ever narrows the tier — see `meta_limits.effective_daily_cap()`. `source` is `coexistence`/`new`; `status` is `pending_signup → online` (or `verifying`/`offline`/`failed`). |
| `templates` | one row per (shop, template_key). A template is per-WABA, so the same skeleton must be **created separately in every salon's WABA** and approved separately. `name` (`kairo_promo_v1`) is deliberately the same across shops — Meta scopes names per-WABA — so status updates key on `(shop_id, name)`, never on name alone. `status` tracks Meta's verdict. |
| `outbound_messages` | queue **and** log in one table. A row is written the moment a send is planned and never deleted: `queued → sending → sent → delivered/read`, or `suppressed`/`failed`/`cancelled`. `template_name` + `template_language` are how Meta addresses a template; `variables` (jsonb) are its parameters; `preview` is the rendered body, stored so a row says what the customer actually read. `provider_sid` is Meta's `wamid`. `price_usd` is our **send-time estimate** — Meta never reports an amount — and `credits_charged` is unused: the salon pays Meta directly. |

**Three things in this schema are load-bearing and easy to undo by accident:**

- **`senders.access_token` is the whole credential.** Unlike the Twilio
  subaccount model there is no shared parent secret: this per-customer
  business token is the complete authority over one salon's WhatsApp. Lose it
  and we hold a WABA we can neither reach nor unsubscribe from, which is why
  `complete()` persists it *before* making any call that uses it.
- **`senders.waba_id` is the only tenant router.** Meta posts every
  customer's traffic to one app-level webhook and identifies the shop solely
  by `entry[].id`. Hence the unique index on it.
- **`outbound_messages.status = 'sending'` is a claim, not a provider state.**
  The drip sweep flips rows into it in the same statement that selects them
  (`whatsapp_queries.claim_due`, `FOR UPDATE … SKIP LOCKED`), so two
  overlapping ticks or two Fly machines can never both send the same row. A
  row stuck there — a tick that died mid-send — is requeued by the next
  sweep (`requeue_stuck`), not abandoned.
- **`whatsapp_outbound_campaign_customer_uniq`** (`shop_id, campaign_key,
  customer_id`, partial) is the idempotency: a retried or double-clicked
  campaign enqueue is a no-op, not a second message to the same person.

`business_app_core.ai_token_log.whatsapp_message_id` — the column the SMS
work added and left unused — is now written, by
`token_basket_queries.try_debit_for_message`.

**One read reaches out of this schema entirely.** `whatsapp_queries.monthly_quota`
joins `business_app_core.shops → subscription_plans` for
`whatsapp_monthly_messages` (webapp migration 54, added 2026-08-22): the plan's
monthly send allowance, enforced at enqueue and again at send. It is a
webapp-owned column — this repo reads it and never writes it, and the column
exists before we query it because `migrate-all.yml` runs the parent's
migrations first. A shop with no plan joins to nothing and gets 0, which is the
intended answer, not a missing-data case. Counted against it:
`sent_this_month`, which counts `sent_at` — a message that never left doesn't
spend an allowance.

## Cross-schema references

`voice_agent.calls` FKs into `business_app_core.shops`/`customers`/`appointments` — cross-schema foreign keys are used deliberately rather than duplicating those rows into `voice_agent`. `sms.outbound_messages`/`sms.opt_outs` do the same into `business_app_core.shops`/`customers`, as do all three `whatsapp` tables.

## Connection

`booking_engine/db/connection.py` — a single asyncpg pool (`pool_min_size=2`, `pool_max_size=10`, both from `Settings`). **No `pool.acquire()` timeout is configured anywhere in this codebase** — under enough concurrent calls the pool itself becomes a contention point with no bound on the wait (flagged, not yet actioned, in `CLAUDE.md` §2026-07-24 "Root-caused session 'dead air'..."; not urgent while call volume is near zero).
