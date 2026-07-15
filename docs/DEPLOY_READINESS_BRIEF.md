# Deploy Readiness Brief — 2026-07-16

Status of the standalone repo vs. a live, deployed, Neon-wired voice service.
Written after live testing the Telnyx sub-account and reviewing the OpenAI path.

## TL;DR

- **Code is test-green** (279 passed). Telephony flow is now auto-wired (no manual
  Telnyx portal step). Escalation, service brief, booking authz, and constraints
  are done.
- **Can we register agent tools with OpenAI? YES** — natively supported, no
  alternative needed. It's already done in one code path.
- **The #1 blocker is architectural, not external:** the repo has **two
  divergent agent implementations** that must be unified before it can run
  end-to-end. See "Architecture divergence."
- **External blockers on you:** OpenAI API key + SIP project, Telnyx account
  funding ($0 balance) + IT regulatory docs, a public HTTPS URL, and the Neon
  connection string.

## Telnyx — live test results (read-only, no spend)

Tested with the shared key (ROTATE it — it was in chat).

- **Balance: $0.00** → cannot order numbers or place calls until funded.
- **IT local numbers: ~$2.00/month + ~$2.00 one-time** (verified across samples).
  Toll-free +39 800 ≈ $6/mo. Plus per-minute usage (inbound termination +
  outbound SIP leg to OpenAI) and OpenAI's own audio minutes.
- **Regulatory (the real limit):** IT local numbers require an **end-user
  identity + Proof of Address in Italy** (utility bill/invoice < 3 months).
  Orders go `pending_review` → our `number.status` webhook flips
  `active`/`rejected`. This is per end-user, not per-number.
  - **Friction-saver:** for the *forward* path the DID is **ours**, not the
    salon's — register all DIDs to **one Kairo Italian entity/address** (one KYC
    doc set, reused via a Telnyx requirement group) instead of collecting a
    utility bill per salon. Keeps onboarding low-friction.
- **Account has** a Default Outbound Voice Profile (needed for the SIP leg) and 0
  TeXML apps / 0 numbers.
- **Flow completion (done this session):** provisioning now creates/reuses a
  shared TeXML Application (voice_url = our webhook) and orders each number with
  that `connection_id`, so inbound routes to us automatically. TeXML app
  create/delete verified live.

### Per-customer number: yes
Each shop that enables voice needs its own DID (we identify the shop by the
dialed number). ~$2/mo + $2 setup + usage per shop.

## OpenAI — can we register agents/tools to the LLM?

**Yes, fully.** The Realtime API registers tools at session start; the model
calls them; we execute and stream results back. Confirmed by OpenAI docs and by
this repo already doing it. No alternative provider needed.

Two ways OpenAI accepts a phone call:
1. **Native SIP** (what `voice_texml.py` dials: `sip:$PROJECT_ID@sip.api.openai.com`):
   point the trunk at OpenAI → OpenAI fires a `realtime.call.incoming` webhook →
   you **accept** with session config (`instructions` + `tools`). Native SIP is
   **beta**.
2. **Bridge pattern** (Telnyx/Twilio → your SIP/WS bridge → OpenAI WebSocket):
   the docs' recommended production default in 2026. The `voice_gateway` service
   is shaped to be this bridge.

Reachability check: OpenAI endpoints respond (401 without a key). **We need an
`OPENAI_API_KEY` + a configured SIP project/webhook to test for real.**

## Architecture divergence (resolve this first)

The repo contains **two agent implementations that don't agree**:

| | booking_engine (SIP path) | voice_gateway/realtime.py |
|---|---|---|
| Entry | `voice_texml.py` `<Dial><Sip>` to OpenAI | `/realtime/token` ephemeral `client_secrets` (WebRTC/browser) |
| Tools | 12 authz'd tools (`create_booking`, `modify_booking` w/ phone-authz, `escalate_to_merchant`, …) in `safety_layer` | 5 inline name-based tools (`book_appointment`, `create_customer`, …), no authz |
| Function exec | `/voice/tools/*` endpoints (authz, constraints, audit) | `/realtime/action` proxy, name resolution |
| Prompt/tools registration | `voice/events/session.started` assembles them | inline in the token endpoint |
| Post-call brief/classifier | (designed here) | actually invoked via `/realtime/end` |

Consequences:
- **All the value built recently** (ownership authz, lead-time/service
  constraints, `service_brief`, escalation) lives in **booking_engine** and is
  **not reachable** from the `realtime.py` path.
- The **native-SIP path is incomplete**: nothing handles OpenAI's
  `realtime.call.incoming` webhook or registers the 12 tools on SIP accept.
- Tool schemas in `safety_layer` are `{name, description, parameters}` and need a
  `{"type":"function", …}` wrapper to send to OpenAI (small adapter).

**Decision needed:** pick ONE architecture and wire tool registration into it.
Recommended: **bridge pattern** (Telnyx → `voice_gateway` bridge → OpenAI WS)
using booking_engine's 12 authz'd tools — it's the production-safe default and
reuses everything already built. Native SIP is simpler but beta and currently
unwired.

## Gaps: standalone repo → deployed & Neon-wired

1. **Unify the agent architecture** (above) — the gating task.
2. **Apply migrations to Neon** (before deploy, additive/idempotent):
   `07_shop_config_greeting_overflow.sql`, `08_calls_service_brief.sql`.
3. **Wire to Neon:** set `DATABASE_URL` (prod) + `CI_SCHEMA_SOURCE_URL` (CI clones
   the live schema). Run the `tests/live_db` + migration-shape tests against a
   Neon branch — they're the 14 skipped/33 errored tests locally (no DB).
4. **Deploy target + public URL:** Lambda + HTTP API Gateway per memory; set
   `PUBLIC_BASE_URL` so the TeXML webhook + OpenAI SIP webhook are reachable
   over HTTPS.
5. **Secrets in the deployed env:** `OPENAI_API_KEY`, `OPENAI_SIP_PROJECT_ID`,
   `OPENAI_TOOL_SECRET`, `CONTROL_PLANE_SECRET`, `TELNYX_API_KEY`,
   `TELNYX_PUBLIC_KEY`, `DATABASE_URL`.
6. **Telnyx account:** fund it; complete IT regulatory (one Kairo entity, reused);
   the TeXML app auto-creates on first provision now.
7. **Harden the Telnyx webhook:** `voice_telnyx_webhooks.py` does **not** verify
   the `Telnyx-Signature` — spoofable. Small TDD fix (we have `TELNYX_PUBLIC_KEY`).
8. **Webapp (separate repo/branch):** render the Action Center tile
   (`/voice/memos/{shop}/count`), the `service_brief` on request/transcript
   cards, and the overflow-greeting editor. Contracts are in place.
9. **PR the branch:** `feat/voice-forwarding-overflow` (7 commits) is unmerged;
   `feat/staff-duration-overrides` also parked.

## Blocked on you (to test end-to-end)

- **OpenAI API key + SIP project** (webhook configured in the OpenAI dashboard).
- **Fund Telnyx** + decide the regulatory entity for IT DIDs.
- **A public HTTPS URL** (deploy or a tunnel) for the two webhooks.
- **Neon connection string** for the live-DB tests.
- **Architecture decision:** native SIP vs bridge (recommend bridge).

## Costs at a glance (per active salon)

`~$2/mo DID + ~$2 one-time + per-minute (Telnyx inbound + Telnyx→OpenAI SIP +
OpenAI realtime audio)`. Model each salon's minutes to size the per-call cost;
the DID rental itself is negligible.
