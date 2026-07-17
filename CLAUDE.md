# Project History

A running log of architectural decisions and the reasoning behind them, kept
so future work (by anyone, human or Claude) doesn't have to rediscover the
same trade-offs. Newest entry on top. Don't rewrite old entries when they're
superseded — add a new entry and note what changed and why; the old entry
stays as the record of what was true and decided at the time.

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
