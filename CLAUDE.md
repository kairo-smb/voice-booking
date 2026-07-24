# Project History

A running log of architectural decisions and the reasoning behind them, kept
so future work (by anyone, human or Claude) doesn't have to rediscover the
same trade-offs. Newest entry on top. Don't rewrite old entries when they're
superseded — add a new entry and note what changed and why; the old entry
stays as the record of what was true and decided at the time.

---

## 2026-07-24 — Root-caused session "dead air": tool calls were self-proxying over real HTTPS

**Finding (from real QA Fly logs + a call-graph trace, not a guess):** a live
SIP test call's access log showed every `/voice/tools/{name}` route
responding in under a second on its own, yet 20-36s gaps between successive
tool invocations across the session — pointing at per-call overhead rather
than DB query time. Traced the call graph from `mcp_server.py::_call_tool`
down: `execute_tool()` was invoked with `base_url=settings.public_base_url`,
meaning that on **every single tool call** (all 12 tools, not just the
ATTESA-gated ones), the app made a real outbound HTTPS request to its own
public Fly URL to reach a route defined in the exact same running process —
`/mcp` and `/voice/tools/*` are mounted on the same `FastAPI` app
(`booking_engine/api/app.py`). Worse, `execute_tool` opens a fresh
`httpx.AsyncClient` per call (no connection pooling across calls), so this
paid a full TCP+TLS handshake, every time, on top of whatever Fly's
edge/proxy hairpin routing added to reach "itself." This is architecture-wide
dead air, not a one-tool problem — it explains why the whole session felt
uniformly sluggish rather than one specific tool being slow.

**Fix:** `execute_tool()` already had an in-process fast path
(`ASGITransport(app=app)`) — every existing test already used it, production
never did. `booking_engine/mcp_server.py` now holds a module-level reference
to the live app (`set_app()`, called once from `create_app()` right after
`app.mount("/mcp", mcp_asgi)`), and `_call_tool` passes `app=_app_ref`
instead of `base_url=`. Tool dispatch is now a direct in-process ASGI call —
same code path tests already exercised, zero new dependency, no behavior
change to auth/constraint logic (still the same `/voice/tools/{name}`
handlers). `settings.public_base_url` is untouched elsewhere (still used to
build the URL OpenAI dials for `/mcp/` itself).

**Not chased further (no evidence either way):** the same log trace showed
`get_services` called twice, 2s apart, in one session. Access logs have no
request bodies, so there's no way to tell from this evidence alone whether
that's a legitimate filter-refinement follow-up call or a duplicate — flagged
for awareness, not treated as a bug.

**Bundled into the same commit** (per-owner instruction, matching the
established pattern this session of committing verified parallel work
together): an unrelated, already-complete, already-tested change from a
separate concurrent effort — `SIP_TEST_FALLBACK_SHOP_ID` (`booking_engine/config.py`,
`voice_openai.py`) lets a raw softphone SIP test call route to a fixed shop
when there's no `X-Shop-Id` header (Twilio normally adds that header when
proxying; a bare SIP client dialing OpenAI directly has no such translation
layer). QA-only (empty by default; production calls with no shop id are
still rejected as unroutable). Pairs with the already-committed
`scripts/run_sip_test.sh` / `scripts/print_sip_test_uri.py`.

## 2026-07-21 — Reviewed voice-config WIP commit; found and closed a missing-migration gap for tone_id

**Context:** commit `1c45663` ("wip: remove voice preset/language fields from
voice config") was committed earlier the same day marked "full review still
pending" — removing the legacy `shops.welcome_message/tone_instructions/
personality/special_instructions` columns and the old
`/shops/{id}/voice/config` endpoints in favor of `voice_agent.shop_config` +
`voice_agent.voice_tones`. This entry is that review.

**The core removal was correct** — endpoints, field mapping, and
`voice_preset` vocabulary (alloy/ash/ballad/coral/echo/sage/shimmer/verse)
all matched the intended shape. Smaller issues fixed alongside: a stray
indentation bug in `prompt_assembler.py`; `docs/DEPLOY_VOICE_AGENT.md`'s
smoke test still curling the removed endpoint; added the missing
`GET /voice/config/tones` route (the DB query `list_preset_tones()` existed,
nothing routed to it — needed for the webapp tone picker).

**The real finding: `tone_id` had no migration behind it and would have
crashed on first use.** `voice_config.py`'s `ConfigPatch`/`_PATCHABLE_FIELDS`
and `prompt_assembler.py` already assumed `shop_config.tone_id` (a UUID FK
into `voice_agent.voice_tones`) existed. It didn't — migration 04 only ever
created `shop_config.tone_preset text DEFAULT 'warm'`, and no `voice_tones`
table was ever defined in any committed migration. `upsert_config` builds
its SQL from whatever field names it's given, so any PATCH setting `tone_id`
would fail with "column does not exist"; reads would always silently return
`None` and fall back to the default tone. This was invisible because the
only tests for it (`test_migration_06.py`, `test_voice_tones_db.py` — a
fully-specified spec for exactly this gap, evidently written earlier and
never acted on) require a live `DATABASE_URL` and had been skipping locally
this whole time.

**Fixed by writing the missing `06_voice_tones.sql`** to match those tests'
exact expectations (8 seeded presets: professionale, amichevole, efficiente,
luxury, tecnico, casual, empatico, conciso). Also added
`10_shop_config_voice_preset_default.sql` (the `voice_preset` column default
was still the pre-rename `'warm_female'`).

**Before writing migration 06 against real Neon, checked the actual QA
schema first — good thing, because it had already drifted.** Someone had
previously added `voice_tones` + `shop_config.tone_id` directly against QA
(presumably via Neon MCP, same as this session), seeded with the correct 8
presets, but as a **plain full `UNIQUE(name)` index**, not the partial
`UNIQUE(name) WHERE is_preset` index this migration file originally assumed.
Running the file as originally written would have hit the exact
"no unique or exclusion constraint matching the ON CONFLICT specification"
bug class recorded in the 2026-07-18 entry below — caught before running
anything by inspecting `information_schema`/`pg_indexes` on QA first, not by
trial and error against production data. Rewrote the migration to match the
already-live shape (full unique index, `description NOT NULL`, `is_preset
DEFAULT true`) instead of imposing a redundant second index. Verified
against a local Postgres both fresh and in a reconstructed copy of QA's
exact drifted state (table pre-existing, `tone_preset` not yet dropped)
before touching real data.

**Applied to the real QA branch** (`br-damp-recipe-agnys6xk`): confirmed
`tone_preset` dropped, `tone_id` intact on the one existing shop_config row
(pointing at a real tone someone had already set manually), preset count
still exactly 8 (no duplicates from re-seeding), `voice_preset` backfilled
from `warm_female` to `verse`.

**Companion webapp changes** (separate repo, own commit): swapped all
callers off the removed endpoint, fixed `VoiceShopConfig`'s type (was still
declaring the pre-rename `voice_preset` enum and a fictional `tone_preset`
field — the Settings tab's voice dropdown and tone picker were both
non-functional), added the tone picker UI, converged a second dead
duplicate of the config UI (the Inbox "Configuration" tab, independently
broken the same way) onto the same working component, and fixed the
onboarding wizard writing its welcome-message step into the now-dead
`shops` columns instead of `shop_config.greeting_after_disclosure`.

## 2026-07-21 — SIP call supervisor: production fix for the mute-after-MCP blocker (built)

**Supersedes the status of the earlier 2026-07-21 "Realtime + hosted MCP does
NOT auto-speak tool results" entry below**, which recorded production as
blocked and the fix as "not built here — needs its own design." It is now
built and merged to `QA` (flag-gated off). That entry stays as the record of
the diagnosis; this one records the fix.

**Decision:** Added `booking_engine/services/call_supervisor.py` — a per-call
**server-side Realtime control WebSocket**. On SIP accept, `voice_openai.py`
calls `maybe_supervise(call_id, settings)`, which spawns an `asyncio` task that
opens `wss://api.openai.com/v1/realtime?call_id=...` (confirmed mechanism: after
`/accept` you may connect a control WS keyed by `call_id` and send/receive
events), sends `response.create` to greet, and sends another after each
`mcp_call` completes so the agent voices tool results instead of going mute.
This is the server-side equivalent of the harness data-channel loop. It also
fixes a **second** prod gap the same way: nothing previously triggered the
opening greeting on a real call either.

**Why a WS worker and not a config flag:** OpenAI's Realtime hosted-MCP does
not auto-continue after a tool result (proven from a live event trace —
`response.done` fires before the tool returns, then the result item is orphaned
with no successor response), and no session setting is known to change that.
The SIP accept path is fire-and-forget with no connection to the session, so
the only place to inject `response.create` is a control WS we open ourselves.

**Design choices worth knowing:**
- Pure `decide(event, state)` core (greet/nudge/dedup) is the unit-testable
  heart; `supervise()` is thin async glue with an injectable `connect=` seam so
  tests use a fake WS (no live OpenAI). A `nudge_pending` guard triggers exactly
  one `response.create` per tool result — a single event type
  (`response.output_item.done`, not also `mcp_call.completed`) plus the guard
  removes both double-nudge sources (parallel tools, duplicate events).
- Best-effort + isolated: call audio is OpenAI↔Twilio, independent of this WS,
  so a worker crash degrades only that call (no greeting/nudge), never drops it.
  One reconnect on drop; `greeted` prevents re-greeting on reconnect.
- Per-call structured stdout logging (one JSON line/event, with tool
  `latency_ms`) for `fly logs` debugging.
- **Gated behind `ENABLE_CALL_SUPERVISOR` (default False)** — prod path
  unchanged until flipped.

**Caught by review before merge (subagent-driven development, final
whole-branch review):** the fire-and-forget `asyncio.create_task` return value
was originally un-retained — asyncio holds only a *weak* reference, so the
supervisor task could be garbage-collected mid-call (intermittently
reintroducing the exact mute/no-greeting bug it fixes). Fixed with a
module-level `set()` + `add_done_callback(discard)`. Also added the
reconnect / no-re-greet / give-up / non-JSON-frame tests that the first cut
lacked (18 supervisor tests total).

**Still needed before enabling the flag:** only observable with live
telephony — do the manual QA SIP-call check (confirm a `supervisor.greeted`
line in `fly logs`, a greeting, and post-tool speech with `latency_ms`), then
enable in prod. Deferred non-goals: barge-in/turn handling, post-call outcome
capture from the event stream, DB event persistence. The `QA` merge commit is
local as of this writing (not pushed to `origin`). Still no live inbound calls
(Twilio unfunded).

**Also fixed in passing:** the earlier trailing-slash fix (commit `f7363c2`)
had left `tests/voice_gateway/test_voice_test_server.py` asserting the old
`/mcp` URL; updated to `/mcp/`.

- Spec: `docs/superpowers/specs/2026-07-21-sip-call-supervisor-design.md`.
  Plan: `docs/superpowers/plans/2026-07-21-sip-call-supervisor.md`.
- Built in worktree `.worktrees/sip-call-supervisor`, branch
  `feat/sip-call-supervisor`, merged to `QA` (`--no-ff`).

## 2026-07-21 — Cost-gated pricing + multi-service/multi-staff bookings

**Decision:** Two voice-tool refinements, built together (subagent-driven
development, spec + code-quality review per task, 9 tasks): (1) `get_services`
now only returns `price_cents` when the caller passes `include_price=true`
(default omitted) — a `SAFETY_PROMPT` rule tells the model to set that flag
only when the customer explicitly asks about cost, never to volunteer it.
(2) `check_availability`/`create_booking` now take an ordered list of
`services`/`legs` (`{service_id, staff_id?}`) instead of a single
`service_id`/`staff_id`, so a visit can require multiple services performed
by different staff in sequence (e.g. colore by one stylist, piega by
another) — a plain single-service booking is just a one-element list, no
separate code path.

**Why additive, not a rewrite of the existing single-service functions:**
`booking_engine/db/queries.py::get_available_slots`/`create_appointment`
are shared ground-truth functions also used by
`booking_engine/api/routes/availability.py`/`appointments.py`, outside the
voice-agent's scope. Rather than change their signatures (which would have
touched unrelated callers and their tests), added new, additive functions —
`get_available_slot_chains` (multi-leg chain search, recursive first-fit
backtracking across staff×time×legs) and `create_appointment_chain`
(multi-leg write: one `appointments` row + one `appointment_services` row
per leg, each with its own `staff_id`/`start_time`, matching what the schema
already supports — `appointment_services.staff_id`/`start_time` existed
before this work but nothing in the voice layer used them). The wrapper
layer (`voice_tool_queries.py::find_availability`/`insert_booking_locked`)
picks single-leg (reuses the untouched original functions, zero behavior
change) vs. multi-leg (routes to the new chain functions) based on request
size — confirmed byte-for-byte via MD5 hash that `get_available_slots` has
zero diff from before this work.

**Gap constant:** `MAX_GAP_MINUTES = 20` (`booking_engine/services/booking_constraints.py`)
— the max idle time allowed between the end of one service and the start of
the next in a chain. Fixed, not per-shop configurable; nothing has asked for
it to vary.

**Ordering is the model's own domain knowledge, not a stored rule:** no
service-dependency/ordering table exists in the schema, and none was added.
`SAFETY_PROMPT` instead tells the model to sequence multi-service requests
by hairdressing convention (color/chemical treatments before cut/styling)
unless the customer states a different order — the system enforces
whatever order the `services`/`legs` list arrives in, it doesn't know *why*
a given order is correct.

**Bugs caught by review before merge, fixed same-session:**
- `get_available_slot_chains` originally compared a deduplicated
  services-found count against a non-deduplicated requested-id count,
  misreporting a chain that legitimately repeats the same `service_id`
  twice (different staff) as "unknown service."
- Missing a final sort of returned chains by `slot_start` — the spec had
  promised "sorted by proximity to preferred_when" but the first cut never
  sorted before returning, so a later chain from one staff member could come
  back ahead of an earlier one from another.
- The chain-extension search only tried the single earliest candidate start
  time per staff/window; if that exact instant conflicted with an existing
  appointment, the search abandoned that staff/window entirely instead of
  trying later starts still within `MAX_GAP_MINUTES` — could report "no
  availability" when a valid slot existed. Fixed to step through the full
  gap-allowed window in 5-minute increments.
- `create_appointment_chain` had an unguarded dict lookup that would
  `KeyError` (crashing the tool call) if a leg's service became
  inactive/missing between the availability check and the write — same bug
  class as the 2026-07-17 FK-crash entry below. Now raises a clean
  `RuntimeError("invalid_service")` instead, propagating through to a
  `{"ok": false, "error": "invalid_service"}` tool response.
- `insert_booking_locked` initially added an unconditional extra DB
  round-trip (re-fetching durations) for every booking, including the
  dominant single-service case, which previously needed zero extra queries.
  Fixed so single-leg bookings read `start_time`/`end_time` straight off
  what `create_appointment` already returns, matching pre-existing
  behavior; only multi-leg bookings still pay for the re-fetch (necessary,
  since `create_appointment_chain` only returns the parent `appointments`
  row, not a per-leg breakdown).
- Found by a final whole-branch review (not per-task review — only visible
  once the pieces were viewed together): `get_available_slot_chains`'s
  search broke out the instant it collected `max_results` chains, but
  candidate generation is staff-major within a day (exhausts one staff's
  whole day before trying the next, no `ORDER BY` on eligible staff), so a
  later slot from the first-iterated staff member could be returned instead
  of a genuinely earlier slot from a staff member iterated later — the
  earlier "add a final sort" fix above only reordered whatever had already
  been collected, it couldn't fix a search that stopped too early. Reproduced
  concretely (two eligible staff, first busy until 14:00, second free from
  09:00 — search returned the 14:00 slot). Fixed so the search only stops at
  a day boundary, never mid-day (since candidates within one day aren't
  time-ordered across staff, but candidates across different days always
  are) — this fully closes the "sorted by proximity" promise the earlier fix
  only partially delivered on. Regression test added with 2+ eligible staff
  for the first leg specifically, since none of the existing tests exercised
  that case.

**Flagged, not actioned:** `create_appointment_chain` validates each leg's
staff against *existing* DB rows but never validates the legs in one request
*against each other* — nothing stops a `create_booking` call (if the model
sent a fabricated `legs` array instead of copying a real `check_availability`
result verbatim) from assigning the same staff member to two overlapping
legs in the same request, which would write two conflicting
`appointment_services` rows. The single-leg path has an equivalent
"trust what's given, no re-validation beyond one overlap check" limitation
today; this just extends the same accepted risk shape to N legs. Not fixed
here — same reasoning as `update_customer_from_call`'s missing shop check in
the 2026-07-17 entry below (a policy/validation decision, not a narrow
error-handling fix); worth a fast-follow (pairwise leg overlap + ordering +
`gap_within_limit` check before the insert loop) given this codebase already
treats voice-agent tool arguments as untrusted input elsewhere.

**Still needed:** `tests/live_db/*` (the tests that exercise the real
dispatch chain against a real Neon-shaped DB, no mocking) were updated for
the new wire shape but — per this repo's existing convention — could not be
run in this environment (`DATABASE_URL` not set here). The chain algorithm
itself is currently only verified by mocked unit tests
(`tests/booking_engine/test_queries.py`); run the `live_db` suite against
the QA Neon branch to confirm `get_available_slot_chains`/
`create_appointment_chain` behave correctly against real staff schedules
and real overlapping-appointment data before treating this as fully proven
end-to-end.
- Spec: `docs/superpowers/specs/2026-07-21-cost-gating-multi-staff-booking-design.md`.
  Plan: `docs/superpowers/plans/2026-07-21-cost-gating-multi-staff-booking.md`.
- Built in worktree `.worktrees/cost-gating-multi-staff-booking`, branch
  `feat/cost-gating-multi-staff-booking`.

## 2026-07-21 — Realtime + hosted MCP does NOT auto-speak tool results (prod blocker)

**Finding (from a live harness event trace, not docs):** in the Realtime API
with a hosted `mcp` tool, after the model emits a tool call its response
**ends** (`response.done` fires *before* the tool even returns), the tool
executes server-side, `response.mcp_call.completed` + `response.output_item.done`
deliver the result — and then **nothing**. OpenAI does not open a new response
to voice the result, so the agent goes silent after every fetch. This
contradicts the Responses-API "hosted MCP auto-continues" behaviour and the
assumption recorded in the 2026-07-21 trailing-slash entry's follow-up; the
raw event log (`response.done` at output_index 0, then an orphaned
`mcp_call.completed` at index 1 with no successor response) disproves it.

**Harness fix (shipped):** `voice_test_static/index.html` now tracks
`responseActive` (`response.created`→true, `response.done`→false) and, on
`response.output_item.done` for an `mcp_call`, sends `{"type":"response.create"}`
after a 100 ms guard when no response is active — nudging the model to speak
the result (or chain the next tool). This is the standard Realtime tool-result
pattern we were simply never sending.

**Production is NOT fixed and is blocked by this:** the SIP path
(`voice_openai.py` → `accept_sip_call`) is fire-and-forget — it POSTs the accept
config to `/v1/realtime/calls/{id}/accept` and holds **no** websocket/event
connection to the session, so there is no client to send `response.create`.
Before real calls go live, production needs either (a) a server-side control
WebSocket to each realtime call that injects `response.create` on tool
completion, or (b) an OpenAI session/config mechanism that makes hosted-MCP
tool results auto-continue (unverified one exists). Not built here — needs its
own design. Not urgent only because Twilio is still unfunded (no live inbound
calls yet).

## 2026-07-21 — MCP server_url must carry a trailing slash (prod + harness)

**Decision:** Point every OpenAI Realtime `mcp` tool `server_url` at `/mcp/`
(trailing slash), not `/mcp`, in both the production SIP path
(`voice_openai.py`) and the local test harness (`voice_test_server.py`).

**Why:** `app.mount("/mcp", mcp_asgi)` makes Starlette 307-redirect `/mcp` →
`/mcp/`. Root-caused from the QA Fly logs (`fly logs -a
kairo-booking-engine-qa`): during a harness call, OpenAI's Realtime MCP client
POSTed to bare `/mcp`, got `307`, and **never followed the redirect** — three
tool attempts in one call all showed as `307` with no subsequent `/mcp/ 200`
or `/voice/tools/*`. So the tool never executed; the model narrated "verifico
subito le disponibilità…" and then hung waiting for data that never came
(looked like "MCP idle + never returns to the customer"). Direct probes to
`/mcp/` return `200 ok:true`, confirming the redirect — not auth, not the DB —
was the sole failure. Note this contradicts an earlier assumption (recorded
then disproven here) that OpenAI follows the 307; it does not for the tool-call
POST body. Production had the identical bug on line 68 of `voice_openai.py`;
fixed there too even though real SIP calls aren't live yet (Twilio unfunded).

**Also:** hardened the harness event log (`voice_test_static/index.html`) to
surface MCP handshake failures (`mcp_list_tools.failed`) and a catch-all
firehose (`DEBUG_EVENTS`) of every unhandled Realtime event, so a silently
non-firing tool is visible next time instead of looking like "idle".

## 2026-07-18 — CI/CD: ephemeral Neon branches, seed-data bug fix, Lambda removal

**Decision:** Replace the local-Postgres-container + `pg_dump --schema-only`
CI mechanism with real, throwaway Neon branches (copy-on-write children of
`production`) across all three DB-touching workflows — `ci.yml` (PR checks),
`deploy-qa.yml` (QA release), `deploy-fly-prod.yml` (prod release) — matching
the pattern already in use by the `webapp` repo, which shares this same Neon
project. QA and prod releases now validate migrations + `tests/live_db/`
against a disposable branch *before* ever touching the real QA branch or
production. Also deleted the superseded AWS Lambda deploy path and fixed the
seed-script bug that had silently blocked every CI/QA/prod run since
2026-07-16.

**Why this was needed:** every CI, QA, and prod-release run had been failing
since the Telnyx→Twilio merge, always at the same step:
`ERROR: there is no unique or exclusion constraint matching the ON CONFLICT
specification` on `booking_engine/db/sql/02_seed_data.sql:75`. Root-caused by
querying the real Neon schema directly (not guessing): the local bootstrap
schema (`01_schema.sql`) declares `UNIQUE (staff_id, day_of_week)` on
`staff_schedules`, but the real `business_app_core.staff_schedules` table has
only ever had a plain non-unique index on those columns — the two schemas
had silently diverged, and CI's `db-tests` job was seeding fake fixture data
into a `pg_dump`-cloned copy of the *real* schema, which the seed script was
never written against. Per this file's own 2026-07-16 entry ("shared schemas
are ground truth... revise the schema only if truly impossible"), the fix
is on the query side, not the schema side: `ON CONFLICT (staff_id,
day_of_week) DO NOTHING` → a `WHERE NOT EXISTS (...)` guard, which is
constraint-independent and works identically against both schemas.

**Why ephemeral branches, not just a bug patch:** the failure exposed a
deeper gap — CI was testing against a bare schema clone with fake seed data,
never the real, current shape of the shared `business_app_core` database
that `webapp` and `marketing-engine` also write to. Fixing only the one
broken query would have left that gap open for the next schema drift.
Ephemeral, copy-on-write Neon branches (create → migrate → seed → test →
delete, per run) give CI real prod-shaped data at effectively no cost
(copy-on-write) and no risk (every write lands on a disposable branch,
production is only ever read). This mirrors `webapp/ci.yml`'s existing
PR-time pattern, extended here to also gate the push-triggered QA/prod
release workflows, not just PRs.

**Lambda removal:** `docs/DEPLOY_READINESS_BRIEF.md` had already recorded
"Deploy to Fly.io (decided)" superseding an earlier AWS Lambda plan, but the
Lambda deploy path (`deploy.yml`, `lambda_handler.py`, a Lambda-only
`Dockerfile`, `deploy-booking.sh`, its test, the `mangum` dependency) was
never actually deleted — found and removed as part of this work, since
`deploy.yml` would otherwise have kept firing on every push to `main`
alongside the real Fly deploy.

**Issues caught by review before merge (subagent-driven development, spec +
code-quality review per task):**
- Ephemeral-branch delete failures were originally swallowed with `|| true`
  — silently orphaning branches in the shared Neon project on any transient
  API failure. Changed to emit a visible `::warning::` while still not
  failing the job.
- `deploy-qa.yml`'s ephemeral branch name initially omitted
  `github.run_attempt` (unlike `ci.yml`'s), which would have collided with
  itself on a re-run of a failed workflow. Fixed and backported to
  `deploy-fly-prod.yml` before it shipped with the same gap.
- The Lambda→Fly.io README/deploy-guide rewrite initially overclaimed "both
  services deploy via GitHub Actions" (only Booking Engine does; Voice
  Gateway is manual-only) and implied `CONTROL_PLANE_SECRET` was already
  configured via GitHub Actions secrets when it's actually a Fly app secret
  (`fly secrets set`, separate from CI) — left uncorrected, either could have
  caused a real production 401 or a false assumption about deploy coverage.

**Implementation notes:**
- Spec: `docs/superpowers/specs/2026-07-17-neon-ephemeral-branch-cicd-design.md`.
  Plan: `docs/superpowers/plans/2026-07-17-neon-ephemeral-branch-cicd.md`.
- Built in worktree `.worktrees/neon-ephemeral-branch-cicd`, branch
  `feat/neon-ephemeral-branch-cicd`, PR #4 into `QA`.
- The seed-data fix and the ephemeral-branch CI itself were both verified
  against the real, live Neon project (`kairo`, `falling-bread-89568725`)
  before merge — the seed fix via a throwaway MCP-created branch, tested
  twice for idempotency; the full `ci.yml` flow via two real PR CI runs
  (both green), each provisioning and cleanly deleting a real ephemeral
  branch.

**Still needed:** manually remove now-unused GitHub repo secrets —
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`CONTROL_PLANE_SECRET`, `DEMO_SHOP_ID` (Lambda-only) and
`CI_SCHEMA_SOURCE_URL` (only used by the removed `pg_dump` mechanism) — same
manual, out-of-band cleanup category as the old `TELNYX_*` secrets.

## 2026-07-17 — Live tool-dispatch + security test coverage

**Decision:** Add `tests/live_db/test_tool_dispatch_{reads,writes,security}.py`
— the first tests to exercise the *entire* real dispatch chain
(`execute_tool()` → `/voice/tools/{name}` route → `safety_layer`
authz/constraints → `queries.py`) against real Neon-shaped data, calling
`execute_tool()` directly (the exact function OpenAI's MCP path calls) with
no mocking. 24 new tests: 5 read-tool, 7 write-tool, 12 security (token
integrity, cross-shop authz, phone-mismatch authz, lead-time/past-slot
constraints, unknown-tool-name rejection, malformed/missing-field input,
and the two independently-violable FK scenarios — nonexistent `customer_id`
and nonexistent `staff_id` — each asserted separately).

**Why this gap existed:** `tests/voice_gateway/test_voice_tools_*.py` covers
the route handlers but mocks every DB call; `tests/live_db/*.py` covers the
query layer but calls it directly, below the authz layer. Nothing exercised
`safety_layer`'s authz/constraint logic against a real row or the real
schema — a regression there (e.g. `modify_booking` silently dropping its
phone check) would have passed every existing test. Verified this class of
regression actually gets caught: manually removed the phone-mismatch check
in `booking_authz.py` and confirmed (via a monkeypatch-driven simulation of
the full dispatch path, since the QA branch currently lacks seed data —
see below) that execution then proceeds past the point it should have been
rejected, then restored the check.

**A real production bug was found and fixed along the way, not just a test
gap:** while strengthening a security test for malformed input, discovered
that `create_booking` with a syntactically valid but nonexistent
`customer_id` crashed `execute_tool()` itself with an unhandled
`asyncpg.exceptions.ForeignKeyViolationError`, instead of returning a clean
error — because `insert_booking_locked`
(`booking_engine/db/voice_tool_queries.py`) only caught `SlotConflictError`,
never a customer/staff FK violation from the underlying raw INSERT in
`booking_engine/db/queries.py::create_appointment`. Fixed by catching
`asyncpg.exceptions.ForeignKeyViolationError` alongside the existing
`SlotConflictError` catch, distinguishing `invalid_staff` vs
`invalid_customer` via the exception's `constraint_name` (since both
columns are independently-violable FKs and only `service_id` was already
pre-validated before this call). A stale or hallucinated `customer_id`
reaching this path in production — plausible, since OpenAI supplies tool
arguments — would have crashed a live call's tool invocation before this
fix. Checked the sibling `webapp` repo's own booking-creation code
(`src/lib/db/repositories/appointments.repo.ts`) for the same bug class:
it does NOT share this crash risk — its callers wrap the insert in a
generic catch-all that degrades to a clean 500 (less precise error
messaging, but no crash), and its agent-facing path additionally
pre-validates `customer_id`/`staff_id`/`service_id` against the shop before
ever attempting the insert.

**Two things discovered while writing these tests, worth knowing:**
- `modify_booking`/`cancel_booking` authorize off the **call row's stored
  `shop_id`** (read back via `get_call()`), not the `X-Shop-Id` header the
  MCP dispatch layer sends — cross-shop tests have to insert the call
  itself under the wrong shop, a header alone doesn't reach this check.
- `update_customer_from_call` (`booking_engine/api/routes/voice_tools_identity.py`)
  has **no shop-ownership check at all** — any valid call token can update
  any customer row's `email`/`tags` regardless of which shop the call
  belongs to. Not fixed here (out of scope for a test-coverage plan —
  changing production authz logic is a different, riskier kind of change
  than the narrow FK-crash fix above, which was pure error-handling, not a
  policy decision); flagged as a fast-follow.

**Also flagged, not actioned:** `voice_gateway/` (the old 5-tool, no-authz
agent implementation) is dead code — neither `fly.toml` nor `fly.qa.toml`
build it, only `booking_engine/Dockerfile.fly`. It's still imported by ~6
test files (`tests/voice_gateway/test_call_lifecycle.py`,
`test_db.py`, `test_booking_client.py`, `test_openai_classifier.py`,
`test_realtime_lifecycle.py`, `tests/live_db/test_voice_gateway_persistence.py`),
and the CI workflow files that reference it are the ones another concurrent
effort (the ephemeral-branch-CI spec) is already modifying — deleting it is
a separate follow-up, not part of this work.

**Still needed before this closes the loop end-to-end:** the ephemeral-branch
CI pipeline (separate, in-flight effort — as of this writing, memory records
it's fixed and PR #4 is open into QA, not yet merged) needs to run these
tests in CI. As of this writing the QA Neon branch itself still lacks the
`02_seed_data.sql` fixture rows these tests (and the pre-existing
`tests/live_db/*.py` suite) depend on — every new test currently fails with
`ForeignKeyViolationError` on `shop_id` when run against the QA branch
directly, confirmed not a code defect (verified the failure mode is
exclusively that one exception class). Run locally against the QA branch
via `TEST_DATABASE_URL` once seeded, or wait for the concurrent effort's PR
to merge.
- Spec: `docs/superpowers/specs/2026-07-17-live-tool-dispatch-security-tests-design.md`.
  Plan: `docs/superpowers/plans/2026-07-17-live-tool-dispatch-security-tests.md`.

## 2026-07-16 — Telephony provider: Telnyx → Twilio

**Decision:** Provision Twilio **Estonia (EE) Mobile** numbers as the
Kairo-owned DID for Path 1 (forward) and Path 2 (new) telephony onboarding,
replacing Telnyx Italy local numbers. One Kairo-entity regulatory Bundle,
created once and reused across every shop — no per-salon KYC.

**Why leave Telnyx:** lower onboarding friction was the goal. Turned out
Twilio's Italy catalog has no local/geographic number type at all — only
Mobile at $30/mo — so "provision a mobile number via Twilio" wasn't a
preference, it's the only thing Twilio sells for Italy.

**Why Estonia, not Italy:** the constraint that actually matters for
call-forwarding cost is "within the EU," not "within Italy" — the EU's
intra-member-state calling price cap (capping IT→EU-country calls, vs.
uncapped international rates) applies to any EU member, not just Italy.
Once decoupled from "must be Italian," compared IT/EE/FI/IE/AT/NL/DK:

| Country | Type | Price/mo | KYC address requirement |
|---|---|---|---|
| Italy | Mobile (only option) | $30.00 | anywhere in the world |
| **Estonia** | **Mobile** | **$3.00** | **anywhere in the world** |
| Estonia | Local | $1.00 | anywhere in the world (but not SMS-capable) |
| Finland | Mobile (only option) | $5.00 | must be Finnish (contradicts "no docs" framing) |
| Ireland / Austria / Netherlands | Local | $1-1.60 | must be in-country — new KYC burden, not less |
| Denmark | any type | — | must be Danish for every type — dead end |

Estonia Mobile wins: same friction as Italy Mobile (Kairo's existing Italian
entity docs work directly, no new foreign presence needed), 10x cheaper.

**Why Mobile over Local within Estonia:** Estonia's KYC is identical for
both types (no friction difference), so the deciding factor became SMS
capability — Local numbers aren't SMS-capable in Estonia (true of most EU
geographic ranges), and the plan is to eventually send booking-confirmation
/ opted-in marketing SMS from this number (see below). Local would have
foreclosed that for a $2/mo saving.

**Future SMS/WhatsApp capability (not built yet, deliberately deferred):**
- **Phase 1** (whenever built): send from the raw Twilio number, no
  Alphanumeric Sender ID, salon's name in the message signature/body. Works
  immediately — Italy's Alphanumeric Sender ID pre-registration requirement
  only applies to alphanumeric senders, not plain numeric long codes. Fine
  at small volume (confirmations + ≤10-message opted-in batches) — Italian
  carrier A2P filtering targets bulk/burst campaign patterns, not this.
- **Phase 2** (future, opt-in per shop): a salon can proactively request its
  own registered Alphanumeric Sender ID (its business name as sender).
  Deliberately *not* the default — Twilio confirmed Italy requires
  per-string document registration + carrier vetting for Alphanumeric
  Sender ID, so making every shop's own name the sender would reintroduce
  the per-salon KYC problem this whole design avoids for phone numbers.
- WhatsApp Business messaging is unaffected by any of this — any real
  number can become a WhatsApp sender (Twilio supports voice-OTP
  verification for non-SMS-capable numbers specifically for this case) —
  and is out of scope as its own future feature (Meta business
  verification + template approval, different problem entirely).

**Regulatory bundle model differs from Telnyx's shape:** Telnyx: purchase
first, number goes `pending_review`, webhook flips it active/rejected.
Twilio: the Bundle must be approved *before* a purchase can succeed at all.
Since the Bundle is reused across every DID, that async wait happens once,
at ops setup — not per shop. Every purchase after that is synchronous
(succeeds now or fails outright), so the Telnyx-style async webhook has no
steady-state equivalent and was deleted rather than adapted.

**Implementation notes:**
- The original Twilio client + TwiML webhook existed in this repo before a
  2026-06-06 swap to Telnyx (recoverable from git history at `df9c53a^`) —
  restored and adapted rather than rewritten from scratch.
- Added Twilio `X-Twilio-Signature` request verification — a gap that
  existed under Telnyx too and was never fixed; closed as part of this
  migration rather than carried forward.
- Code review during implementation caught three bugs beyond the original
  plan, all fixed before merge: dead code (`update_telephony_activation`,
  whose only caller was the deleted Telnyx webhook); the provisioned
  `voice_url` was missing its `/api/v1` mount prefix (a bug that predated
  this migration too — every provisioned number's webhook would have 404'd
  in production, so no inbound call would ever have reached the routing
  logic); and `TWILIO_ADDRESS_SID` was read by the provisioning code but
  never wired through CI/deploy-script env vars.
- Spec: `docs/superpowers/specs/2026-07-16-telnyx-to-twilio-migration-design.md`.
  Plan: `docs/superpowers/plans/2026-07-16-telnyx-to-twilio-migration.md`.
- Merged into `feat/voice-forwarding-overflow`; local `QA` branch reset to
  match and pushed (force-with-lease) to `origin/QA`.

**Still needed before this is live:** fund/verify the Twilio account, create
and get approval for the one Kairo-entity regulatory Bundle in the Twilio
Console (manual, out-of-band, not automatable from this repo), add the
`TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_BUNDLE_SID` /
`TWILIO_ADDRESS_SID` GitHub repo secrets (replacing the old `TELNYX_*` ones).
