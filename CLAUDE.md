# Project History

A running log of architectural decisions and the reasoning behind them, kept
so future work (by anyone, human or Claude) doesn't have to rediscover the
same trade-offs. Newest entry on top. Don't rewrite old entries when they're
superseded — add a new entry and note what changed and why; the old entry
stays as the record of what was true and decided at the time.

---

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

**Still needed:** merge PR #4 into `QA`; manually remove now-unused GitHub
repo secrets — `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`CONTROL_PLANE_SECRET`, `DEMO_SHOP_ID` (Lambda-only) and
`CI_SCHEMA_SOURCE_URL` (only used by the removed `pg_dump` mechanism) — same
manual, out-of-band cleanup category as the old `TELNYX_*` secrets.

---

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
