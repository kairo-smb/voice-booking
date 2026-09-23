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

**Do not hand-copy `business_app_core`'s schema into a doc.** That has already gone stale and caused real bugs at least twice (`AGENTS.md` §2026-07-24 "Repo cleanup..." and the schema-mismatch history it references). The accurate, current mapping is `booking_engine/db/queries.py`, exercised against real Neon-shaped data by `tests/live_db/*`. Read that file for column names, not this one.

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

**`customers.marketing_consent*` columns appear in no migration in this repo.** They arrive via the webapp's own migration chain, not this repo's — noted here so nobody re-diagnoses that as a missing-migration bug (found while grounding the 2026-08-14 number-provisioning docs; see `AGENTS.md`).

## `voice_agent` schema — authoritative here

DDL: `booking_engine/db/sql/03_voice_agent_schema.sql` through `13_number_release.sql`, applied in filename order — plus `24_whatsapp_conversations.sql`, which adds `calls.channel` and `service_intake` to this schema while the rest of it is `whatsapp`'s. `01_schema.sql`/`02_seed_data.sql` are a **separate, local-only bootstrap pair** with fake data and unqualified table names — `scripts/migrate.sh` explicitly skips both; never run them against real Neon.

| Table | Added in | Purpose |
|---|---|---|
| `calls` | 03, extended 04/08/24 | one row per **session** — caller number, matched/created customer, outcome, appointment, and (08) a structured hairstylist `service_brief`. Two channels since 24: `channel` is `'voice'` or `'whatsapp'` (see below) |
| `service_intake` | 24 | owner-authored questions the agent must ask before booking a given service, PK `(shop_id, service_id)`. Lives here, not as a column on `business_app_core.services` — that schema belongs to the webapp and this repo does not alter it |
| `call_transcripts` | 03 | per-turn transcript rows for a call |
| `call_events` | 03 | tool-call/event log for a call |
| `shop_telephony` | 04, extended 09/12/13 | provisioned Twilio number per shop, `setup_path` (new/forward), `provider` (defaults `'twilio'` since 09), `health_status`/`health_detail`/`health_checked_at` (12, green/red semaphore — see below), `release_scheduled_at` (13, grace-period release deadline — see below) |
| `shop_config` | 04, extended 06/07/10/25 | Layer 1 voice config: `enabled`, `display_name`, greetings, `voice_preset`, `tone_id` (06, FK to `voice_tones`, replaced an inline `tone_preset` string), `business_hours`, `answer_mode`, token top-up settings. `whatsapp_agent_enabled` (25) is the WhatsApp booking agent's opt-in — **`NOT NULL DEFAULT false`**: a salon that has not asked for a robot must never get one, so silence is the default and speech is the request |
| `callback_memos` | 04 | merchant callback reminders created by `escalate_to_merchant` |
| `auth_events` | 04 | identity-verification audit trail |
| `system_policy` | 04 | disclosure/consent text (seeded it-IT) |
| `voice_tones` | 06 | 8 seeded presets (`is_preset=true`) plus room for shop-authored custom tones (`created_by_shop_id`); seeded names: professionale, amichevole, efficiente, luxury, tecnico, casual, empatico, conciso |
| `number_requests` | 12, extended 13 | one row per shop, PK `shop_id`: self-service Estonian-number regulatory-bundle lifecycle (`status` draft→evaluating→pending_review→approved/rejected→provisioned→**released** (13), the Twilio `regulation_sid`/`bundle_sid`/`end_user_sid`/`document_sid`, `evaluation_errors` jsonb verbatim from Twilio, `rejection_reason`, `released_at`/`released_number` (13, kept for history — see below)). Polled hourly by `POST /api/v1/messaging/tick`. See [Architecture](architecture.md#self-service-number-provisioning-path-2-onboarding) and `AGENTS.md` §2026-08-14. |

**`calls` is a two-channel session table (24), not a telephony table.** It never was one: `shop_id`, `caller_number`, `customer_id`, `customer_match`, `outcome`, `summary` and `appointment_id` describe a conversation, `twilio_call_sid` is UNIQUE but **nullable**, and `duration_seconds` is the only genuinely voice-specific column. Migration 24 adds `channel` (`CHECK (channel IN ('voice','whatsapp'))`, default `'voice'`, so every pre-existing row is correct without a backfill), and a WhatsApp booking conversation is a row here with `channel = 'whatsapp'`.

- **`duration_seconds` is meaningless for `whatsapp`** and is left NULL — a `COMMENT ON COLUMN` in migration 24 says so. It is not zero: a WhatsApp session has no duration, and writing one would poison any average computed over the table. Anything reading it must filter on `channel = 'voice'`.
- **`caller_number` on a WhatsApp row is the customer's WhatsApp number, which Meta has verified** — strictly stronger evidence of identity than a voice call's caller ID, which is why `booking_authz.authorize_booking_change` needs no change to cover the channel.
- **Opening a session:** `booking_engine/db/wa_session_queries.py::open_session(shop_id, phone, customer_id)` returns the open session for that shop+phone, or starts one. "Open" means `ended_at IS NULL` and `started_at` within `wa_routing.SESSION_GAP` (24h), **bound as a parameter, never written as an interval literal** — the same constant `wa_routing.session_messages` splits a message history on, so there is one definition of "one conversation". `shop_id` is part of the lookup because a phone number is not unique across tenants. `customer_match` is `'existing'` when a customer is known, `'unmatched'` otherwise.
- **Why it matters:** minting a call token (`services/call_token.py`) against such a row makes the whole existing tool layer — the 12 tools, `execute_tool`, `authorize_booking_change`, the booking constraints — work over WhatsApp unchanged. Nothing about booking is rebuilt for the channel.
- **Known race, accepted:** two messages arriving at the same instant can both miss the lookup and open two rows. The cost is a duplicate session row, never a wrong answer, and the partial unique index that would prevent it would also forbid the legitimate new session that follows a stale row nobody closed.
- **`outcome = 'escalated'` is how a WhatsApp session is handed to a person** (`wa_session_queries.mark_escalated`, with the reason in `outcome_reason`). Deliberately the same column and the same value the voice agent writes when it gives up (`voice_tools_lifecycle`), so the Inbox has one vocabulary for "a human is needed" across both channels. `whatsapp_thread_queries.thread_list` surfaces it as `escalated`, which `needs_attention` has always read — until the agent existed nothing ever wrote it.

**The agent's handover state is derived, not stored (25).** `wa_session_queries.session_state(call_id, phone)` answers all of `started_at` / `escalated` / `agent_turns` / `human_replied_at` in one query, off rows that already exist — there is no per-thread state table and no `suspended` flag to keep true.

- **`human_replied_at`** is the newest `outbound_messages` row since `started_at` with `origin IN ('kairo','phone')` and `template_name IS NULL AND campaign_key IS NULL`. The template/campaign exclusion matters: a drip campaign firing at a thread is not a person choosing to answer it, and counting it would silence the agent for a reason the owner never chose.
- **`agent_turns`** counts `origin = 'agent'` rows since `started_at` — so the `MAX_SESSION_TURNS` ceiling is per session, and yesterday's conversation cannot exhaust today's. It is also what tells a first turn from a later one. Suppressed and cancelled rows are excluded throughout: a message that never left is neither a reply nor a turn.
- **A self-echo guard** excludes any `phone`/`kairo` row whose `provider_sid` matches one we already recorded as `origin = 'agent'`. Meta's `smb_message_echoes` is understood to mirror only what the owner sent from the WhatsApp Business App, not Cloud API sends — but that is **unverified against a real WABA**, and if it is wrong the failure is silent and total (the agent reads its own reply as the owner and goes quiet after one turn, on every thread, forever). Both paths carry the wamid in `provider_sid`, so the two can be told apart. Confirm on the first real onboarding.

**`service_intake` (24) — the read/write contract, and the 500-character cap.** Accessed only through `booking_engine/db/service_intake_queries.py`; the routes are `GET /api/v1/voice/config/{shop_id}/intake` and `PUT /api/v1/voice/config/{shop_id}/intake/{service_id}` (under voice config, not WhatsApp — the phone agent reads the same rows).

- **`MAX_QUESTIONS_CHARS = 500`, enforced on write, in `normalise()`.** The text is re-read into the agent's prompt on *every turn of every conversation that touches the service*, so an owner who pastes an essay pays for it on each one and the agent's own instructions drown in it. Over the cap is **trimmed then truncated, never refused** — the webapp shows a live character counter against the same number, so a 501-character request means a second client or a stale draft, and storing the first 500 beats a 422 the owner cannot interpret. A UI-only counter would not be a cap.
- **Empty is a legal, meaningful value** — "nothing extra to ask", which is the default for every service and what every service means today. Whitespace-only is stored as `''`, never `'   '`: the latter puts a blank line in the prompt and leaves the owner believing they configured something.
- **`for_services(shop_id, service_ids)` returns only rows with non-empty text**, keyed by service id as text. Absence and emptiness mean the same thing to a prompt, so the two are deliberately not distinguished. An **empty `service_ids` returns `{}` without issuing a statement** — the agent calls this on every turn and a query that can only answer `{}` is a round trip bought for nothing. `for_shop(shop_id)` is the opposite rule and keeps empty rows: the config screen must still show a field the owner deliberately cleared.
- **Tenancy is in the write statement, not around it.** `set_questions` is one `INSERT … SELECT … WHERE EXISTS (SELECT 1 FROM business_app_core.services WHERE id = $2 AND shop_id = $1) … ON CONFLICT (shop_id, service_id) DO UPDATE`, so a service the shop does not own inserts nothing and returns nothing (the route answers 404 — the same answer as an id that does not exist, which is all a caller for another shop is entitled to learn). A check-then-write would leave a window, and the only thing this table feeds is text that goes into a prompt, which is exactly where a cross-tenant leak would be invisible. Reads scope by `shop_id` on their own and never trust the caller to have passed ids it owns.
- **Service *names* are not here and are not returned.** They live in `business_app_core.services`, which this repo reads but does not own, and the config screen has the service list in hand already.

**`shop_telephony`'s health semaphore (12):** `health_status` (`unknown`/`green`/`red`, default `unknown`) records whether a provisioned number still exists at Twilio with its voice webhook pointed at us. A Twilio-unreachable probe deliberately leaves the prior status untouched rather than flipping to red — only a confirmed 404 or voice-webhook drift changes the light (`services/number_health.py::decide_health`). `sms_url` is deliberately not checked — there is no inbound SMS handler any more (STOP handling removed; see `AGENTS.md`), so there is nothing for it to correctly point at.

**Grace-period number release (13), closes the cancellation gap flagged in `AGENTS.md` §2026-08-14.** When a shop's plan lapses (`shops.plan_id` goes `NULL`), the hourly tick doesn't release the number immediately — it stamps `shop_telephony.release_scheduled_at = now() + 14 days` the first time it notices, clears it if the plan comes back before that deadline, and only calls Twilio to release the number once the deadline has passed (`services/number_release.py::decide_release`/`sweep`). The deadline lives here, in `voice_agent`, derived from when *we* first observed the lapse — deliberately not a `plan_lapsed_at` column on `business_app_core.shops`, which is the webapp repo's schema. On release, `shop_telephony`'s row is deleted (Twilio confirms first, row deletion second — a lost Twilio call must not delete a row we're still paying for) and `number_requests.status` moves to `released` with `released_at`/`released_number` stamped so the history survives after the row is gone.

`business_app_core.shops` also gained two columns directly in migration 03: `voice` (default `'alloy'`) and `language` (default `'it'`) — the one place this repo's migrations touch the other schema, both additive/nullable-safe.

## `sms` schema — authoritative here

Added 2026-08-12 (`booking_engine/db/sql/11_sms_schema.sql`), owned by this
repo like `voice_agent`. Phase 1 of a larger SMS/WhatsApp messaging design —
see [Architecture → SMS marketing send](architecture.md#sms-marketing-send-phase-1-of-messaging)
and `AGENTS.md` §2026-08-12. WhatsApp now has its own schema — see
[`whatsapp` schema](#whatsapp-schema--authoritative-here) below.

| Table | Purpose |
|---|---|
| `campaigns` | batch-send container (`draft → approved → sending → sent/cancelled`). **Exists, nothing writes to it yet** — Phase 1 is one-off sends only. |
| `outbound_messages` | one row per send attempt, including refused ones (`status='suppressed'`, `suppressed_reason` — a refusal is always persisted, never silently dropped). `credits_charged`/`price_usd` are the billed figures; `campaign_id IS NULL` means a one-off send. Unique on `(campaign_id, customer_id)` where both are set, so re-running a batch send can't double-message a customer. |
| `opt_outs` | **unused as of the STOP-removal (see `AGENTS.md`).** Kept in the schema, no `DROP TABLE` — intentionally left behind rather than dropped — but nothing reads or writes it any more. |

**STOP handling removed; suppression is `customers.marketing_consent`
alone.** This repo previously reimplemented STOP-keyword parsing in
application code (Twilio's automatic STOP handling doesn't cover the
Estonian DID) and wrote both `sms.opt_outs` and
`customers.marketing_consent = false` on a recognised STOP reply. The owner
decided to remove that entirely — opt-out is now handled in-store, by a
staff member clearing marketing consent in the app. There is no inbound SMS
webhook and no opt-out footer any more; `sms_send.py`'s only suppression
check is `customers.marketing_consent`/`_granted_at`/`_withdrawn_at`. See
`AGENTS.md`'s STOP-removal entry for the full reasoning, including the
explicit note that this is a weaker position under Italian marketing rules,
accepted as the owner's decision.

**Gap worth knowing:** `business_app_core.customers.marketing_consent`,
`_granted_at`, `_withdrawn_at`, `_source` exist on the live database but
appear in **no migration file inside this repo** — they were added through
the webapp's own migration chain, not this repo's. Grepping only this repo
for those columns will come up empty; they're real, just owned elsewhere —
same ownership-boundary caution as the rest of `business_app_core` above.

**SMS sends charge the basket over HTTP — this repo no longer writes
`ai_token_log` (or any spend table).** A send's credit cost (`2×` the
Twilio price, `send_credits`) is recorded locally on
`sms.outbound_messages.credits_charged`, but the actual basket deduction is a
POST to the webapp's charge-actual endpoint
(`booking_engine/clients/webapp_credits.py`, `run_type='sms_send'` +
`run_ref=<message_id>`, pre-converted `credits`). The webapp performs the
single locked deduction and writes the ledger row (`ai_run_ledger`); the old
`ai_token_log` rows this repo used to write (`voice_call_id`,
`sms_message_id`, `whatsapp_message_id`) are superseded — the webapp is
backfilling the ledger from `ai_token_log` and dropping the table (see the
webapp's `docs/knowledge/database.md`). The columns exist only so the
backfill can attribute history; nothing in this repo writes them any more.

## `whatsapp` schema — authoritative here

Added 2026-08-21 (`14_whatsapp_schema.sql`), reshaped for Meta Cloud API on
2026-08-24 (`15_whatsapp_meta.sql`). Owned by this repo. See
[Architecture → WhatsApp marketing](architecture.md#whatsapp-marketing-one-waba-per-salon),
[Providers → WhatsApp](providers.md#whatsapp-meta-cloud-api-tech-provider),
[API → WhatsApp](api/whatsapp.md), and `AGENTS.md` §2026-08-24.

| Table | Purpose |
|---|---|
| `senders` | one row per shop: the salon's `waba_id`, `phone_number_id`, the customer-scoped `access_token` from Embedded Signup, `platform_type` (`COEXISTENCE` when the number is also live on the WhatsApp Business App), display name, `quality_rating`, and **two different Meta ceilings**: `messaging_limit` (the volume tier — conversations per *rolling* 24h) and `throughput_level` (messages per second). `daily_cap` is Kairo's own drip rate and only ever narrows the tier — see `meta_limits.effective_daily_cap()`. `source` is always `coexistence` (BYO WABA only — the `new`-provisioning path was removed 2026-08-30, migration 18); `status` is `pending_signup → online` (or `verifying`/`offline`/`failed`). |
| `templates` | one row per (shop, template_key). A template is per-WABA, so the same skeleton must be **created separately in every salon's WABA** and approved separately. `name` (`it_promo_v1`) is deliberately the same across shops — Meta scopes names per-WABA — so status updates key on `(shop_id, name)`, never on name alone. `status` tracks Meta's verdict. `body_hash` (migration `21_whatsapp_template_body_hash.sql`, 2026-09-02) is *which version of the copy* this WABA holds: `status = 'approved'` only says a template with that name passed review, so without it an edited body reached Kairo's WABA and no salon's, silently. NULL = unknown version, treated as stale and re-pushed on the next tick (gated, as ever, on Kairo's WABA having approved that same body). |
| `outbound_messages` | queue **and** log in one table. A row is written the moment a send is planned and never deleted: `queued → sending → sent → delivered/read`, or `suppressed`/`failed`/`cancelled`. `template_name` + `template_language` are how Meta addresses a template; `variables` (jsonb) are its parameters; `preview` is the rendered body, stored so a row says what the customer actually read. `provider_sid` is Meta's `wamid`. `price_usd` is our **send-time estimate** — Meta never reports an amount — and `credits_charged` is unused: the salon pays Meta directly. `initiated_by` (added 2026-09-02, migration `20_whatsapp_audit.sql`) is the staff id who queued the message — NULL for the tick's automation sends, which have no human in the path. `origin` (migration `24_whatsapp_conversations.sql`, 2026-09-21; widened by `25_whatsapp_agent.sql`) is `kairo`, `phone` or `agent` — the three writers a thread has. A `phone` row is a message the **owner** sent from their own WhatsApp Business App, which Meta reports on the `smb_message_echoes` webhook; those rows land `sent` and stay there — no template, no campaign, no provider status lifecycle — and carry the wamid in `provider_sid`. An `agent` row is a reply the WhatsApp booking agent wrote. **That third value is load-bearing, not descriptive:** `wa_session_queries.session_state` reads `kairo`/`phone` as "a human took this thread over, the agent stands down", so an agent recording its replies as `kairo` would read its own last message as the owner arriving and silence itself after exactly one turn. It is also how a session's turn count against `MAX_SESSION_TURNS` is derived, so no per-thread counter column exists. |
| `inbound_messages` | one row per message from a customer (migration `17_whatsapp_inbound.sql`), matched back to the message it answers by phone (`from_phone` == the sent message's `to_phone`), which is what campaign measurement's "replied within 72h" reads. Migration 24 made it a thread: `wa_message_id` is Meta's wamid and **the dedup key** — a unique index, *partial* on `WHERE wa_message_id IS NOT NULL`, so a webhook Meta replays is a no-op while a message that arrives without an id still records; `intent`/`confidence` are the routing verdict (filled at write time, `confidence = 1.0`, when the customer tapped a button whose id we defined — then no classifier runs at all); `transcript` is a voice note's text and never overwrites `body`; `read_at` is the Inbox's unread state, and an echo from the owner's phone clears it. `max(received_at)` per phone **is** the 24h service window — nothing else feeds it. |
| `interaction_history` | **a view** (migration `26_whatsapp_retention.sql`, 2026-09-21), not a table. One row per inbound message: the router's verdict (`intent`, `confidence`, `summary`, `text` as `coalesce(transcript, body)`) beside how the session it belongs to ended (`call_id`, `outcome`, `outcome_reason`, `appointment_id`). "Did the router get it right?" is a join — the verdict and the outcome live in different tables — and a view cannot drift from the truth, which a summary table would the first time a backfill was skipped. Session membership is **not** `received_at >= started_at`: the worker opens the session row ~2s *after* the message that caused it is already committed, so the first message of every conversation — the one carrying the verdict — precedes its own session. The span is read the way `wa_routing.session_messages` reads one: not after `coalesce(ended_at, started_at + 24h)`, and not from a session that started more than 24h later (that gap is by definition a different request). 24h is `wa_routing.SESSION_GAP`, written as a literal because a view takes no parameters — the one place in this feature where the two are not bound together. LEFT JOIN: a message that never reached a session is exactly the kind this is for. |
| `routing_corrections` | **a view** (same migration). The disambiguation menu is a labelling machine and nobody designed it as one: when the model is not confident we send buttons, and the customer's tap is stored as a verdict with `confidence = 1.0`. So every low-confidence message followed by a tap is a case the model got wrong **plus the correct label, supplied by the person who wrote the message** — a human-verified eval set for the routing prompt, accumulating for free since the menu shipped. The miss is `intent IS NULL AND confidence IS NOT NULL` (the classifier ran and did not name it), deliberately **not** `confidence < 0.7`: that would copy `wa_routing.ROUTING_CONFIDENCE` into SQL to drift there. The tap must be the *immediately* following inbound message from that phone, within 24h — which is what stops a miss being paired with a label from a different conversation, since a customer returning days later types before tapping and a stale button tapped in a later session falls outside the bound. `correct_intent` includes `other` (routes to a human): "no handler covers this" is a correct label too. |
| `audit_events` | who-did-what for WABA actions (migration `20_whatsapp_audit.sql`, added 2026-09-02): one row per campaign enqueue/cancel, automation config, onboarding step, or template-ensure. `event` names the action; `actor_id` is the acting staff id (NULL = the tick/system acted; **no FK** so a deleted staff row never drops the trail); `source` is the webapp surface (`composer`/`touchpoint`/`offer`); `status`/`http_status`/`error_message` record the outcome, with sanitized `request`/`response` (never recipient lists, never the single-use onboarding `code`). Complements `outbound_messages` (which trails the per-message outcome) — this log's insert is fail-open by design: a dead audit DB must not break the send it logs. |

**Three things in this schema are load-bearing and easy to undo by accident:**

- **`senders.access_token` is the whole credential, and it can expire.**
  Unlike the Twilio subaccount model there is no shared parent secret: this
  per-customer business token is the complete authority over one salon's
  WhatsApp. Lose it and we hold a WABA we can neither reach nor unsubscribe
  from, which is why `complete()` persists it *before* making any call that
  uses it. Whether it expires is a property of the **Embedded Signup Login
  Configuration**, not of this code — Kairo's mints 60-day tokens, so
  `token_expires_at` (migration `22_whatsapp_token_expiry.sql`) records
  Meta's `expires_in` from the code exchange and `GET /whatsapp/status`
  returns it. NULL means Meta reported no expiry, never "unknown": existing
  rows are not backfilled. **Nothing renews it** — a business token is minted
  by the salon completing the popup, so recovery is asking them to reconnect,
  which is why the date has to be visible before the sends start failing.
- **`senders.waba_id` is the only tenant router.** Meta posts every
  customer's traffic to one app-level webhook and identifies the shop solely
  by `entry[].id`. Hence the unique index on it.
- **The subject-access artifact covers both directions** (2026-09-21).
  `customer_campaign_messages` — behind `GET /whatsapp/messages/{shop_id}`,
  the webapp's Anagrafiche → Campagne tab and the GDPR "what do you hold about
  me" answer — used to return only what we *sent*, plus the holdout campaigns.
  Once customers write back, half the record is personal data they authored,
  so inbound is UNIONed in, tagged `direction`. A voice note appears as
  `coalesce(transcript, body)`: the transcript is what we actually hold, and
  an empty row would answer "nothing" about a message we have the words of.
  Holdout rows carry `direction IS NULL` — they were never sent in either
  direction, and calling them `out` would assert a send that never happened.
- **`outbound_messages.status = 'sending'` is a claim, not a provider state.**
  The drip sweep flips rows into it in the same statement that selects them
  (`whatsapp_queries.claim_due`, `FOR UPDATE … SKIP LOCKED`), so two
  overlapping ticks or two Fly machines can never both send the same row. A
  row stuck there — a tick that died mid-send — is requeued by the next
  sweep (`requeue_stuck`), not abandoned.
- **`whatsapp_outbound_campaign_customer_uniq`** (`shop_id, campaign_key,
  customer_id`, partial) is the idempotency: a retried or double-clicked
  campaign enqueue is a no-op, not a second message to the same person.

**Messages are kept six months, and nothing else in this schema expires**
(2026-09-21, `services/messaging/wa_retention.py`, `RETENTION = 183 days`).
This is the first retention policy the feature has had — before it, message
bodies accumulated forever, which the design doc flagged as an open GDPR gap.
Three things about the sweep are deliberate:

- **Both directions go together, in one statement.** Half a conversation is
  still personal data and is no longer readable as a conversation, so the
  inbound and outbound deletes are CTEs of a single statement sharing one
  snapshot and one commit. What stays is everything newer than the cutoff —
  a conversation truncated at six months, which is the policy, not a halving.
- **The batch is counted in threads** (`BATCH_THREADS = 500`), not rows,
  because the unit that must not be split is the conversation: a row limit
  could fill up mid-thread and leave the other half for the next run. 500
  threads is a few thousand rows, small enough that the statement never holds
  locks on the two busiest tables for long, and at one tick an hour a year of
  backlog drains in days. Re-running immediately is a no-op by construction.
- **`voice_agent.calls` is untouched.** That row is the business record of an
  appointment being made; it outlives the chat that produced it and is not
  this policy's to expire. It is also why `interaction_history` LEFT JOINs:
  an old session can outlive its messages.

Both views above therefore see six months by construction, which is the
intended scope for a post-mortem and for an eval set alike.

**No Kairo-side debit happens for a WhatsApp send** (the salon pays Meta
directly), so nothing here ever writes `ai_token_log` or charges the basket on
this path. The SMS-only charge path is `token_basket_queries.try_debit_for_message`
→ `booking_engine/clients/webapp_credits.py`, an HTTP POST to the webapp (see
the [`sms` schema](#sms-schema--authoritative-here) section above); the
`whatsapp_message_id` column on `ai_token_log` was only ever written by the
old local debit implementation, which is gone.

**No read reaches out of this schema for a limit any more (2026-08-24).**
`whatsapp_queries.monthly_quota` used to join
`business_app_core.shops → subscription_plans` for `whatsapp_monthly_messages`
(webapp migration 54). It is deleted: Meta bills the salon's own card under the
Tech Provider model, so a Kairo-side ceiling recovered no cost of ours. The
webapp column survives, unread — dropping it would be destructive and it is
harmless. Meta's own tier (`senders.messaging_limit` → `meta_limits`) is now
the only volume ceiling, and it is one this repo already reads.

**`sent_today`, `sent_this_month` and `recently_contacted` count marketing
only**, joining `whatsapp.templates` on `(shop_id, name)` for
`category = 'MARKETING'`. `sent_last_24h` counts everything, because Meta's
tier is measured in business-initiated conversations including utility. Getting
this backwards breaks nothing and silently corrupts every number, so the split
is pinned by tests rather than left to a comment.

## Cross-schema references

`voice_agent.calls` FKs into `business_app_core.shops`/`customers`/`appointments` — cross-schema foreign keys are used deliberately rather than duplicating those rows into `voice_agent`. `sms.outbound_messages`/`sms.opt_outs` do the same into `business_app_core.shops`/`customers`, as do all three `whatsapp` tables.

## Connection

`booking_engine/db/connection.py` — a single asyncpg pool (`pool_min_size=2`, `pool_max_size=10`, both from `Settings`). **No `pool.acquire()` timeout is configured anywhere in this codebase** — under enough concurrent calls the pool itself becomes a contention point with no bound on the wait (flagged, not yet actioned, in `AGENTS.md` §2026-07-24 "Root-caused session 'dead air'..."; not urgent while call volume is near zero).
