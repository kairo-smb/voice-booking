# Ephemeral Redis call sessions — design

## Why

The voice gateway keeps each in-flight call's state in a process-local dict
(`app.state.call_sessions`, `voice_gateway/api/routes/realtime.py`). Every
follow-up request for a call — `/action`, `/transcript`, `/end` — looks the
session up by `call_id` in that dict. This is correct only while every
request for a given call lands on the same machine that created it.

Two goals break that assumption:

1. **Parallel calls across machines.** `fly.toml` runs `auto_stop_machines`
   with `min_machines_running = 0` and a per-machine `hard_limit = 250`.
   Under concurrent load Fly can start a second machine, and it load-balances
   per request (not sticky). A call whose `/token` landed on machine A but
   whose `/end` lands on machine B misses the dict → the call is never
   finalized: no outcome, no `ended_at`, the `voice_agent.calls` row (under
   today's design) stays open forever. `/transcript` misses drop turns
   silently. This is data loss, not answer-mixing — each request already has
   its own `call_id`, so calls never cross-contaminate; they just get lost.

2. **Scale-to-zero.** We want to keep `min_machines_running = 0` on both Fly
   apps (the first rings of an inbound call boot the machine). In-process
   state can't survive that model at all.

Separately, the current write pattern is wrong for our data-retention
posture: **every transcript turn is written durably to Neon**
(`voice_agent.call_transcripts`) and every function call to
`voice_agent.call_events` (`voice_gateway/call_lifecycle.py`). Neon ends up
being the permanent, per-turn store of the full raw conversation — the
opposite of data minimisation, and Neon sits in the per-turn hot path.

This redesign inverts both: the live conversation lives in an **ephemeral,
machine-shared Redis** keyed by `call_id`; Neon receives only the derived
**summary** at hangup, plus the bookings that were always written durably at
action time. The result is reliable across machines and scale-to-zero, is
cheaper, and stores no raw conversation beyond a short debug window.

## Non-goals / out of scope

- Twilio media wiring, WebRTC, or the OpenAI Realtime session itself — audio
  goes client↔OpenAI directly; unchanged.
- The webapp Inbox rendering change is described here for coordination but is
  implemented in the `webapp` repo, not this one.
- Any change to `business_app_core` (the shared schema). Off-limits per
  `CLAUDE.md`. Only `voice_agent` (voice-owned, transient) is touched.
- An abandoned-call sweeper (see Decisions — deliberately deferred).

## Current state (ground truth)

`CallSession` (`voice_gateway/call_lifecycle.py`) is already a write-through
cache over Neon. Its fields and where they are persisted today:

| Field | Persisted today in |
|---|---|
| `id`, `shop_id`, `caller_number`, `twilio_call_sid`, `customer_id`, `customer_match`, `started_at` | `voice_agent.calls` row at `start()` |
| `transcript[]` | one row per turn in `voice_agent.call_transcripts` (`append_turn()`) |
| function calls | rows in `voice_agent.call_events` (`log_event()`) |
| `appointment_id` | written at `finalize()` |
| outcome / summary | written at `finalize()` via `classify_call` |

The dict is touched in exactly four places in `realtime.py`: created at
`/token` (line ~171), read at `/action` (~208) and `/transcript` (~414),
popped at `/end` (~433). No other code holds a reference.

FK note: `voice_agent.call_transcripts` and `voice_agent.call_events` FK to
`voice_agent.calls(id)`. `voice_agent.callback_memos` and
`voice_agent.auth_events` also FK to `calls(id)` but the gateway does **not**
write them today — confirmed by grep. See the tripwire in Decisions.

## Design

### Store: Upstash Redis on Fly, co-located

Provision managed Upstash Redis via `fly redis` for both prod
(`kairo-booking-engine`) and QA (`kairo-booking-engine-qa`), created inside
Fly (Frankfurt) so it is co-located with the machines for lowest latency and
minimal cross-service data transfer. Connection string injected as the
`REDIS_URL` secret.

Rationale over the alternatives (studied 2026-07):
- **DynamoDB** — rejected. Its only real edge is free native TTL, and that
  advantage evaporates now that the whole stack is Fly-only: it would mean
  standing up an AWS account, IAM credentials, and cross-cloud calls solely
  for ephemeral session state.
- **Self-hosted Redis / Valkey on a Fly Machine** — rejected. Reintroduces
  lifecycle management and does not scale to zero; a co-located managed
  Upstash instance is the native Fly option.
- **Upstash Redis (chosen)** — native `fly redis` integration, pay-as-you-go
  ($0.20 / 100k commands, first 500k/mo + 256 MB free) so ~$0 idle — which
  is the "start/stop during no-call" property expressed as pay-per-use —
  native list ops for transcript turns, and accurate explicit TTL for the
  debug-retention window.

The in-memory `app.state.call_sessions` dict is **removed**. All session
state resolves through Redis, so any machine can serve any request for any
call. Cross-machine routing and scale-to-zero both stop being problems.

### Per-call key layout

TTL = **5 days (`432000s`)**, set **once** at call creation (never per turn —
each `EXPIRE` is a billable command, and per-turn TTL would double command
count for no benefit).

- `call:{call_id}` — hash (or single JSON string): `shop_id`,
  `caller_number`, `twilio_call_sid`, `customer_id`, `customer_match`,
  `started_at`, `appointment_id`, and agent **action-feedback notes**
  (e.g. `"booked appointment {id} for {service} at {time}"`).
- `call:{call_id}:turns` — Redis list; `RPUSH` one entry per turn
  (`{role, text, at}`).

### Write flow (inverted)

- **During the call → Redis only.** No per-turn Neon writes.
  `voice_agent.call_transcripts` and `voice_agent.call_events` stop being
  written.
- **Bookings → Neon immediately, through the booking engine** — unchanged.
  The money-action is durable at action time, independent of the transcript.
  After a successful booking the handler also appends an action-feedback note
  into `call:{call_id}` so the end-of-call summary has the concrete facts.
- **`/end` → summarise + persist once.** Read `call:{call_id}` +
  `call:{call_id}:turns`, run the existing `classify_call` classifier
  (`{outcome, outcome_reason, summary, service_brief}`), and write **one**
  finalized `voice_agent.calls` row (summary/outcome/booking link, **no
  transcript**). Do **not** delete the Redis keys — let the 5-day TTL expire
  the raw transcript. This preserves a debug window and saves a `DEL`.

### Component boundaries

`CallSession` is refactored so its persistence backend is Redis, not
per-turn Postgres, while keeping the same public surface the routes already
call (`start`, `attach_new_customer`, `append_turn`, `log_event`,
`set_appointment`, `finalize`). Concretely:

- `start()` — runs caller→customer reconciliation (unchanged SQL against
  `business_app_core`), then writes the meta hash + TTL to Redis. It no
  longer inserts a `voice_agent.calls` row.
- `append_turn()` / `log_event()` — `RPUSH` to Redis instead of `INSERT`.
- `set_appointment()` / `attach_new_customer()` — update the Redis meta hash
  and append an action-feedback note.
- `finalize()` — reads Redis, calls `classify_call`, writes the single
  `voice_agent.calls` row (`INSERT`, since no start-row exists).
- A `CallSession.load(call_id)` classmethod rehydrates the object from Redis
  so any machine can service `/action`, `/transcript`, and `/end`. The route
  handlers call `load()` instead of the dict `.get()`.

This keeps the routes thin and confines the store swap to `CallSession` +
one small Redis client module.

### `/end` idempotency

Because state is now shared and `/end` no longer `pop`s from a local dict, a
duplicate `/end` (client retry, double hang-up) could re-run the classifier
and re-insert the row. Guard it: `finalize()` is a no-op if a
`voice_agent.calls` row for that `call_id` already exists (or set a
`call:{call_id}:finalized` marker in Redis and check it first). Pick the
Redis marker — one `SET NX` avoids a Neon round-trip on the happy path.

## Schema / migration (`voice_agent` only)

New migration file `booking_engine/db/sql/05_*.sql` (next number in
sequence):

- Stop writing `voice_agent.call_transcripts` and `voice_agent.call_events`.
  Leave the tables in place but dormant (drop deferred) to avoid coordinating
  a destructive change with the webapp in the same step — the webapp Inbox
  currently reads `call_transcripts`. Once the webapp reads transcripts from
  Redis (or only shows summaries), a follow-up migration can drop them.
- `voice_agent.calls` is unchanged structurally — it already has
  `summary`, `outcome`, `outcome_reason`, `ended_at`, `duration_seconds`,
  `appointment_id`. The only behavioural change is that its row is now
  `INSERT`ed once at `/end` rather than `INSERT` at start + `UPDATE` at end.

Per `CLAUDE.md` / memory, `business_app_core` is untouched; `voice_agent` is
voice-owned transient state and safe to evolve.

## Config / infra

- `fly redis create` for prod + QA; add `REDIS_URL` to Fly secrets on both
  apps and to the CI/deploy env wiring (`.github/workflows/deploy-*.yml`,
  `scripts/deploy-voice.sh`).
- Add `redis` (async `redis-py`) to the voice gateway requirements.
- New env: `CALL_SESSION_TTL_SECONDS=432000` (5 days), `REDIS_URL`.
- Redis client initialised in the app lifespan (`voice_gateway/api/app.py`)
  and stored on `app.state.redis`; closed on shutdown. Mirrors how the
  booking client is wired today.
- `min_machines_running = 0` stays on both Fly apps. Redis is external
  managed serverless, so it neither blocks scale-to-zero nor costs while
  idle.

## GDPR posture (documented, not legal advice)

- **Data minimisation + storage limitation:** raw conversation is never
  written to the permanent store. Neon keeps only the derived summary plus
  the booking (a legitimate business record). Raw transcript exists only in
  Redis under a **stated 5-day retention window** (the TTL), for debugging,
  then auto-expires.
- This is *relief, not exemption* — the summary and booking still contain
  personal data (name, phone, appointment), so GDPR still applies.
- The larger processor surface is **OpenAI Realtime processing the live
  audio**, which needs a DPA regardless of what we store. Out of scope here
  but noted so it isn't mistaken as solved by this change.

## Cost

- Redis PAYG: ~60 commands/call (RPUSH turns + a few meta writes + one bulk
  read at `/end`); 10k calls/mo ≈ ~600k commands ≈ near the 500k free tier,
  i.e. pennies. $0 idle.
- Neon: strictly fewer writes than today — one row per call instead of N
  transcript rows + M event rows. Cheaper.
- Net: cheaper than the current design *and* reliable across machines /
  scale-to-zero.

## Testing

- Unit: `CallSession` against a fake/mock Redis (`fakeredis` async or a
  monkeypatched client) — `start` writes meta + TTL once; `append_turn`
  RPUSHes; `load` round-trips a session; `finalize` reads Redis, calls a
  stubbed classifier, writes exactly one `calls` row; duplicate `finalize`
  is a no-op.
- Route: `/transcript` and `/end` for a `call_id` created on a *different*
  simulated machine (fresh app instance, shared fake Redis) succeed — the
  cross-machine regression that motivated this. One runnable check that
  fails if session state is process-local again.
- TTL: assert the 5-day expiry is set once at creation and not re-issued per
  turn (guards the cost gotcha).

## Decisions (defaults; revisit if noted)

- **Write the `calls` row once at `/end`, not at start.** Simpler, fewer
  writes. Cost: no "in-progress" row, so the Inbox lists calls only after
  they end. Accepted.
  - **Tripwire:** if `callback_memos` or `auth_events` (which FK to
    `calls(id)`) ever get written *mid-call*, the `calls` row must move back
    to call-start (insert at `start()`, update at `finalize()`), because the
    FK needs the parent row to exist during the call. Not the case today.
- **No abandoned-call sweeper in v1.** A crash before `/end` loses that
  call's summary, but the booking is already durable and the partial
  transcript sits in Redis for 5 days. Add a TTL-swept finalizer only if
  abandoned calls prove to matter.
- **Inbox drawer:** summary from Neon always; raw transcript rendered from
  Redis if still inside the 5-day window (webapp change, cross-repo).
- **Store:** Upstash Redis via `fly redis`, co-located Frankfurt.

## Risks

- **Redis unavailability mid-call.** If Redis is down, turns can't be
  buffered and `/end` can't summarise. Bookings still succeed (they don't
  touch Redis). Mitigation: log and degrade — a failed `/end` summarisation
  should not 500 the hang-up; the booking is already safe. Accept
  summary-loss on Redis outage, same failure class as the crash case.
- **Webapp coupling.** The Inbox currently reads `call_transcripts`. Until
  the webapp is updated, its transcript drawer will show nothing for new
  calls (rows stop being written). Sequence the webapp change alongside, or
  ship summary-only first.
