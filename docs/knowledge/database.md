# Database

> **Maintenance rule:** a schema migration that adds/removes/renames a `voice_agent` table or column updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Ownership boundary

Two schemas in one Neon Postgres database:

| Schema | Owner | This repo's access |
|---|---|---|
| `business_app_core` | the `webapp` Control Plane repo | reads/writes narrowly through `booking_engine/db/queries.py`; **never alters its DDL** |
| `voice_agent` | this repo | owns it fully — DDL lives in `booking_engine/db/sql/`, applied in order by `scripts/migrate.sh` |

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

**Other stable conventions:** soft deletes via `is_active = false` (never hard-delete a row with dependent appointments); timezone is hardcoded `Europe/Rome` for all slot calculations (`ZoneInfo("Europe/Rome")` in `queries.py`); IDs are Postgres-generated (`gen_random_uuid()`), never assigned by application code.

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

## Cross-schema references

`voice_agent.calls` FKs into `business_app_core.shops`/`customers`/`appointments` — cross-schema foreign keys are used deliberately rather than duplicating those rows into `voice_agent`.

## Connection

`booking_engine/db/connection.py` — a single asyncpg pool (`pool_min_size=2`, `pool_max_size=10`, both from `Settings`). **No `pool.acquire()` timeout is configured anywhere in this codebase** — under enough concurrent calls the pool itself becomes a contention point with no bound on the wait (flagged, not yet actioned, in `CLAUDE.md` §2026-07-21 "Cost-gated pricing..."; not urgent while call volume is near zero).
