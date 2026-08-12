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

## `voice_agent` schema — authoritative here

DDL: `booking_engine/db/sql/03_voice_agent_schema.sql` through `10_shop_config_voice_preset_default.sql`, applied in filename order. `01_schema.sql`/`02_seed_data.sql` are a **separate, local-only bootstrap pair** with fake data and unqualified table names — `scripts/migrate.sh` explicitly skips both; never run them against real Neon.

| Table | Added in | Purpose |
|---|---|---|
| `calls` | 03, extended 04/08 | one row per inbound call — caller number, matched/created customer, outcome, and (08) a structured hairstylist `service_brief` |
| `call_transcripts` | 03 | per-turn transcript rows for a call |
| `call_events` | 03 | tool-call/event log for a call |
| `shop_telephony` | 04, extended 09 | provisioned Twilio number per shop, `setup_path` (new/forward), `provider` (defaults `'twilio'` since 09) |
| `shop_config` | 04, extended 06/07/10 | Layer 1 voice config: `enabled`, `display_name`, greetings, `voice_preset`, `tone_id` (06, FK to `voice_tones`, replaced an inline `tone_preset` string), `business_hours`, `answer_mode`, token top-up settings |
| `callback_memos` | 04 | merchant callback reminders created by `escalate_to_merchant` |
| `auth_events` | 04 | identity-verification audit trail |
| `system_policy` | 04 | disclosure/consent text (seeded it-IT) |
| `voice_tones` | 06 | 8 seeded presets (`is_preset=true`) plus room for shop-authored custom tones (`created_by_shop_id`); seeded names: professionale, amichevole, efficiente, luxury, tecnico, casual, empatico, conciso |

`business_app_core.shops` also gained two columns directly in migration 03: `voice` (default `'alloy'`) and `language` (default `'it'`) — the one place this repo's migrations touch the other schema, both additive/nullable-safe.

## `sms` schema — authoritative here

Added 2026-08-12 (`booking_engine/db/sql/11_sms_schema.sql`), owned by this
repo like `voice_agent`. Phase 1 of a larger SMS/WhatsApp messaging design —
see [Architecture → SMS marketing send](architecture.md#sms-marketing-send-phase-1-of-messaging)
and `CLAUDE.md` §2026-08-12. WhatsApp (a separate `whatsapp` schema) is
designed but not built; this repo currently has `sms` only.

| Table | Purpose |
|---|---|
| `campaigns` | batch-send container (`draft → approved → sending → sent/cancelled`). **Exists, nothing writes to it yet** — Phase 1 is one-off sends only. |
| `outbound_messages` | one row per send attempt, including refused ones (`status='suppressed'`, `suppressed_reason` — a refusal is always persisted, never silently dropped). `credits_charged`/`price_usd` are the billed figures; `campaign_id IS NULL` means a one-off send. Unique on `(campaign_id, customer_id)` where both are set, so re-running a batch send can't double-message a customer. |
| `opt_outs` | suppression list of last resort, keyed `(shop_id, phone_normalized)`. |

**Why `opt_outs` exists alongside `customers.marketing_consent`:** a STOP
reply must be honoured even from a phone number that matches no
`customers` row (a wrong number, an imported list, a deleted customer) —
there is nothing in `business_app_core` to flip in that case. When the
phone *does* match a customer, the STOP handler
(`services/messaging/sms_inbound.py`) writes **both**: the `opt_outs` row
(works regardless of a match) and `customers.marketing_consent = false` /
`marketing_consent_withdrawn_at = now()` /
`marketing_consent_source = 'sms_stop'` (keeps the webapp's own consent UI
honest — it reads `business_app_core` directly). `sms.opt_outs` is also the
legal evidence trail for the Garante.

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

## Cross-schema references

`voice_agent.calls` FKs into `business_app_core.shops`/`customers`/`appointments` — cross-schema foreign keys are used deliberately rather than duplicating those rows into `voice_agent`. `sms.outbound_messages`/`sms.opt_outs` do the same into `business_app_core.shops`/`customers`.

## Connection

`booking_engine/db/connection.py` — a single asyncpg pool (`pool_min_size=2`, `pool_max_size=10`, both from `Settings`). **No `pool.acquire()` timeout is configured anywhere in this codebase** — under enough concurrent calls the pool itself becomes a contention point with no bound on the wait (flagged, not yet actioned, in `CLAUDE.md` §2026-07-24 "Root-caused session 'dead air'..."; not urgent while call volume is near zero).
