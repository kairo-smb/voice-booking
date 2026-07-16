# Telnyx → Twilio Migration — Design

## Context

Plan A's telephony provider was originally Twilio, swapped to Telnyx on
2026-06-06 (commit `7505ecf`; old Twilio files deleted in `df9c53a`). We're
swapping back to Twilio for lower onboarding friction. Nothing is live yet —
Telnyx sub-account balance is $0, no numbers have been purchased, no shop has
a real DID (per `docs/DEPLOY_READINESS_BRIEF.md`). **This is a clean
pre-launch vendor swap, not a live migration.** No dual-provider run, no DID
porting, no data backfill.

The flow this preserves unchanged: two onboarding patterns per shop —
**Path 1 (forward)**: salon's existing number carrier-forwards to a
Kairo-owned DID. **Path 2 (new)**: salon gets a new Kairo-owned number
directly. Only the vendor and the specifics of the DID underneath change.

## Number choice: Twilio Estonia (EE) Mobile

The provisioned DID is never customer-facing for Path 1 (the salon's own
published number is what customers dial; the Kairo DID is purely the
backend forwarding target the AI agent answers). For Path 2 it's the number
given to the salon to publish — country/type is otherwise invisible to the
booking flow.

**Decision: Twilio Estonia (EE) Mobile number**, one Kairo entity's
regulatory bundle reused across every DID (see below), $3.00/mo.

Rejected alternatives and why:

| Option | Price/mo | Why not |
|---|---|---|
| Twilio Italy Mobile | $30.00 | Italy has **no local/geographic number type** on Twilio at all — mobile is the only API-purchasable type. Address-anywhere KYC (good), but 10x the cost of EE for no added benefit once EE is on the table. |
| Twilio Ireland / Austria / Netherlands / Denmark local | $1-1.60 | All require a **genuine in-country address** for local (and, for Denmark, even mobile/toll-free) — Kairo has no non-Italian entity, so these add KYC friction rather than removing it. |
| Twilio Finland | $5 (mobile only) | No local type; mobile requires a Finnish address despite generic docs suggesting otherwise. Dead end. |
| Twilio Estonia Local | $1.00 | Same KYC treatment as EE Mobile (address-anywhere-in-the-world applies to both), but **not SMS-capable** — ruled out once one-way SMS (confirmations + small marketing blasts) became a requirement. |

Why the EU-country constraint is satisfied by any EU member, not just Italy:
the intra-EU calling price cap (regulation capping calls from one EU member
state to another at ~€0.19/min since May 2019) is what keeps the salon's
carrier-forwarding cost low — it applies EU-to-EU, not IT-to-IT specifically.
Estonia qualifies same as Italy would.

**This should be validated with a real (sandboxed) Twilio bundle submission
before full commitment** — the KYC findings above come from Twilio's public
regulatory-guidelines pages, which carry Twilio's own "not legal advice"
disclaimer, not from a live test.

## Regulatory bundle model (differs from Telnyx's shape)

Telnyx: purchase first, number goes `pending_review`, webhook flips it to
`active`/`rejected` per-number.

Twilio: the KYC **Bundle** must be approved *before* a number purchase can
succeed at all. Since we reuse **one Kairo entity's bundle** across every
DID (same pattern already validated for Telnyx — one Kairo IT entity, no
per-salon KYC), the async approval wait happens **once, at ops setup**, not
per shop. After that one-time approval:

- `POST /voice/numbers/provision` becomes **synchronous** — the Twilio
  purchase call either succeeds immediately (number active) or fails
  outright (e.g. bundle not yet approved, number no longer available).
- The Telnyx-style `pending_review` webhook has no steady-state equivalent
  to replace. `activation_status` stays `active`/`pending_review`/`rejected`
  in the schema (no migration needed either way — the column already allows
  these values), but the **normal path never produces `pending_review`**
  post-bundle-approval. Surface purchase failures as a synchronous error
  response from `provision()`, not an async status flip.
- Creating and monitoring the one-time Kairo entity bundle is an **ops
  runbook step** (Twilio Console or a one-off script), not a new API
  endpoint — nothing in the per-shop provisioning flow needs to manage it.

## What we do NOT need Kairo/salon documentation clarified

Per-shop KYC is not required — the DID is Kairo's own infrastructure
(Kairo's AI agent operates it; the salon never sees or dials it directly).
One Kairo entity's bundle, reused for every DID, exactly mirrors what's
already validated for Telnyx.

## Code mapping

The original Twilio implementation existed in this repo before the June
swap and is recoverable from git history (`df9c53a^`) — restore and adapt
rather than write from scratch; the routing logic (`decide_session`,
fallback-loop-safety) is nearly identical to what's live today in
`voice_texml.py`.

| Today (Telnyx) | Becomes (Twilio) |
|---|---|
| `booking_engine/clients/telnyx_numbers.py` | `booking_engine/clients/twilio_numbers.py` — restore from `df9c53a^`, change `.local.list(...)` → `.mobile.list(...)`. Drop `ensure_texml_application` entirely: Twilio sets `voice_url` directly on the number at purchase time, no shared "Application" resource needed. |
| `booking_engine/api/routes/voice_texml.py` (`/voice/texml/incoming`) | `booking_engine/api/routes/voice_twiml.py` (`/voice/twiml/incoming`) — restore from `df9c53a^`, same `decide_session`/fallback logic. Twilio's webhook does send legacy `Called`/`Caller`/`CallSid` params alongside `To`/`From` (confirmed by the original Plan-A implementation using them) — verify against current Twilio docs during implementation, not just this precedent. |
| `booking_engine/api/routes/voice_telnyx_webhooks.py` | **Deleted, not renamed.** Per-number async status has no steady-state equivalent (see above). |
| `config.py`: `telnyx_api_key`, `telnyx_public_key`, `telnyx_default_country` | `twilio_account_sid`, `twilio_auth_token`, `twilio_default_country = "EE"` |
| CI secrets `TELNYX_API_KEY` / `TELNYX_PUBLIC_KEY` (`.github/workflows/deploy.yml`) | `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` |
| `shop_telephony.provider` default `'telnyx'` | Data-only change to `'twilio'` — free-text column, no CHECK constraint, no migration needed. |
| `pyproject.toml`: `telnyx` dependency | Drop it. `twilio>=9.0` is already present (never removed after the 2026-06-06 swap). |
| No webhook signature verification (open gap, never implemented for Telnyx either) | Add `X-Twilio-Signature` validation via `twilio.request_validator.RequestValidator` on `/voice/twiml/incoming` — closes this gap as part of the swap rather than carrying it forward. |

`booking_engine/api/routes/voice_openai.py` has a doc comment referencing
"the X-Shop-Id SIP header Telnyx set" — the header itself is set by our own
`_dial_sip` construction (vendor-agnostic), only the comment's wording needs
updating; no logic change.

## SMS capability (number property only — not a build item)

The chosen number type (EE Mobile) is SMS-capable, which keeps the door open
for one-way SMS (booking confirmations, small opted-in marketing batches
≤10 messages) directly from this number with no Alphanumeric Sender ID
needed at that volume — carrier A2P filtering targets bulk/burst campaign
patterns, not this scale.

**Explicitly out of scope for this migration:** building the SMS-sending
feature itself, opt-in capture, and any Alphanumeric Sender ID work. This
spec only ensures the underlying number *can* do it later. Two things to
revisit if/when that feature is scoped:

- If marketing volume grows materially beyond small batches, revisit
  Alphanumeric Sender ID registration (Twilio-managed per-country carrier
  due diligence) rather than continuing on the raw long code.
- Customers will see a `+372` (Estonian) number as the SMS sender once this
  ships, not a branded name — acceptable at small opted-in scale, worth
  addressing (via Sender ID) if it becomes a trust/branding concern.
- If two-way SMS (customer replies) is ever needed, that's a different
  requirement than what's designed here and needs its own scoping.

WhatsApp Business messaging is unaffected by this number choice (any real
number can become a WhatsApp sender via voice-OTP verification) and is
entirely out of scope here — it's a separate future feature with its own
Meta business-verification and template-approval requirements.

## Testing

Restore and adapt the deleted test files from `df9c53a^`:
`test_twilio_numbers_client.py`, `test_voice_twiml_webhook.py`. Update
`test_voice_telephony_routes.py` and `test_forwarding_heartbeat.py` fixtures
from Telnyx response/webhook shapes to Twilio's. Delete
`test_voice_telnyx_webhooks.py` (no replacement — see code mapping above).

## Rollout

Single branch, no dual-provider transition:
1. Ops: create + get approval for the one Kairo entity regulatory bundle in
   Twilio (one-time, outside this codebase).
2. Code: client + route + config + CI secrets + tests, as mapped above.
3. Merge. No data migration, no existing shops to touch.
