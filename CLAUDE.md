# Project History

A running log of architectural decisions and the reasoning behind them, kept
so future work (by anyone, human or Claude) doesn't have to rediscover the
same trade-offs. Newest entry on top. Don't rewrite old entries when they're
superseded — add a new entry and note what changed and why; the old entry
stays as the record of what was true and decided at the time.

---

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
