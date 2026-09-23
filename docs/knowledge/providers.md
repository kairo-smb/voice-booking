# Providers

Every external service this repo talks to: purpose, auth, and the hard rules that keep the integration safe. Verified against the actual client code.

> **Maintenance rule:** adding/removing/changing a provider integration updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Cross-service environment contract

Three services call each other on the same shared database — this repo
(`booking_engine`), the webapp, and marketing-engine — plus OpenAI, which calls
*us*. The credential names are not a free-for-all: each one is named for the
thing it authenticates **into**, and one of them has a format rule that has
already caused a wrong turn during a merge (see the `/api/v1` note below).
Nothing generates these; every row is typed by hand into four Fly apps and the
webapp's host, which is why they are written down once here.

| Variable | Lives on | Value | Presented by |
|---|---|---|---|
| `CONTROL_PLANE_SECRET` | booking engine | random | the webapp holds the only copy, as `VOICE_AGENT_SECRET` below |
| `VOICE_AGENT_API_URL` | **webapp** | voice-booking base **including `/api/v1`** | webapp → this repo's `/api/v1/*` |
| `VOICE_AGENT_SECRET` | **webapp** | = this repo's `CONTROL_PLANE_SECRET` | webapp → this repo's `/api/v1/*` **and** `/voice/memos/*` |
| `VOICE_AGENT_TOOLS_URL` | **marketing-engine** | voice-booking base **with NO `/api/v1`** | engine → this repo's `/voice/tools/*` |
| `VOICE_AGENT_TOOL_SECRET` | booking engine + **marketing-engine** | = this repo's `voice_agent_tool_secret` | OpenAI Realtime (per session), and the engine's booking agent |
| `MARKET_INTEL_API_URL` | booking engine | engine base **with NO `/api/v1`** | this repo → engine `/whatsapp/triage`, `/whatsapp/agent` |
| `MARKET_INTEL_SECRET` | booking engine + marketing-engine + webapp | shared bearer | this repo → engine; both → webapp's credit + notify endpoints |
| `WEBAPP_BASE_URL` | booking engine + marketing-engine | webapp base, no trailing slash | this repo + engine → webapp |
| `JWT_SECRET` | webapp + marketing-engine | **byte-identical** | the engine verifies the webapp's session tokens on the browser-facing chat routes |
| `WHATSAPP_TOKEN_KEY` | booking engine | Fernet key, **distinct per environment** | internal — encrypts `whatsapp.senders.access_token` at rest |

**Two of these are `VOICE_AGENT_*` and they are not interchangeable.** `VOICE_AGENT_API_URL` always ends in `/api/v1`; `VOICE_AGENT_TOOLS_URL` must not. One variable name demanding two shapes is how the wrong value gets copied — which is why there are two names. Copying either into the other's slot produces a 404 on every call, and route tests do not catch it: they mock `fetch` and assert status and body, both of which stay green no matter how wrong the URL is.

**The prefix is *not* a reliable proxy for the credential, so read the routes rather than the shape of the URL.** `api/app.py` mounts seven routers at the root — `voice_openai`, `voice_memos`, `voice_events`, and the four `voice_tools_*` — and they do not share an auth scheme:

| Mounted at root | Guard |
|---|---|
| `/voice/tools/*` (catalog, booking, lifecycle, identity) | `require_tool_token` |
| `/voice/events/*` | `require_tool_token` |
| `/voice/memos/*` | **`require_control_plane_token`** — the webapp calls this one, which is why its proxy strips `/api/v1` off `VOICE_AGENT_API_URL` to reach it |
| `/voice/openai/*` | no route-level dependency: it is the Twilio/OpenAI webhook and verifies its own signature in the handler |

So the durable distinction is the **credential** (control plane = the webapp's app API; tool token = agents), not the prefix. `/api/v1/*` happens to be all-control-plane, but the root is mixed, and `/voice/memos/*` is the case that proves it.

**`VOICE_AGENT_TOOL_SECRET` is deliberately not `CONTROL_PLANE_SECRET`.** They are two credentials for one service because they gate two surfaces with different blast radii: `/voice/tools/*` can create and cancel appointments, `/api/v1/voice/numbers/*` can buy a phone number. Collapsing them to satisfy a "one service, one token" reading would let a leaked agent credential spend money.

**The value never rotates on a rename.** When `openai_tool_secret` became `voice_agent_tool_secret` (2026-09-22) the *name* changed on both sides and the value did not — verified by digest, both reading `0de449382af8070e` on `kairo-booking-engine-qa`, so there was no window where the two ends disagreed. A rename and a rotation at once is two risks wearing one commit.

---

## Twilio

**Purpose:** inbound phone numbers and call routing. `clients/twilio_numbers.py` searches/purchases/releases/fetches EU mobile numbers; `api/routes/voice_twiml.py` handles the per-call dynamic TwiML webhook that routes an inbound call to the right shop and dials it into OpenAI; `clients/twilio_regulatory.py` drives the Regulatory Compliance API (Regulations/EndUsers/SupportingDocuments/Bundles/Evaluations) for self-service number requests.

**Key files:** `booking_engine/clients/twilio_numbers.py`, `booking_engine/clients/twilio_regulatory.py`, `booking_engine/api/routes/voice_twiml.py`, `booking_engine/api/routes/voice_telephony.py` (provisioning + self-service request endpoints), `booking_engine/api/routes/messaging_tick.py` (hourly poll/provision/health-check), `booking_engine/services/number_provisioning.py`, `booking_engine/services/number_health.py`, `booking_engine/services/realtime_session.py::build_sip_uri` (how the shop id is attached to the outbound SIP dial).

**Env vars:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_DEFAULT_COUNTRY` (`EE`), `TWILIO_BUNDLE_SID`, `TWILIO_ADDRESS_SID`. No new env var was needed for self-service provisioning — each salon's bundle SID is created dynamically and stored on its own `number_requests` row, never configured.

**Why Estonia, not Italy:** Estonia Mobile numbers are ~$3/mo vs. Italy Mobile's $30/mo (the only type Twilio sells there). Full country-by-country comparison in `AGENTS.md` §2026-07-16.

**Two regulatory-bundle models coexist, deliberately — read `AGENTS.md` §2026-08-14 before assuming either is dead:**
- **Shared bundle (2026-07-16, still live):** `TWILIO_BUNDLE_SID` — one Kairo-entity bundle, reused via `POST /voice/numbers/provision`. Still used for Path 1 (forwarding) and ops-triggered onboarding.
- **Per-salon bundle (2026-08-14, self-service):** `POST /voice/numbers/request` builds a fresh Regulation→End-User→SupportingDocument→Bundle chain **per shop**, stored on `voice_agent.number_requests`. Twilio's ISV rules forbid reusing Kairo's own business info across customer bundles ("Twilio audits this") — the shared-bundle model above is not a legal substitute for this path, only a narrower carryover for the flows it already served.
- Estonia Mobile's regulation (`RN26dca8d0e541a6c8fce4abd46e518506`) is **business-only** and asks for exactly one End-User field (`business_name`) and one document (`commercial_registrar_excerpt` — an Italian *visura camerale*): no address, VAT, or personal ID. **Sending fields the regulation doesn't request is a known cause of evaluation failure** — don't add them speculatively. The regulation SID is queried at request time (`get_regulation_sid`), never hardcoded; `tests/live_twilio/test_estonia_regulation.py` asserts it still matches, so a failing test there means Estonia's rules changed, not a code bug.
- `Evaluations` is synchronous and its violation objects have **no `description` field** — the explanation is in `failure_reason`. Confirmed against a live noncompliant evaluation; parsing `description` silently returns empty explanations rather than erroring, so this is easy to get wrong without noticing (`clients/twilio_regulatory.py::evaluate`).

**Gotcha:** `twilio_signature_valid()` (`booking_engine/services/twilio_signature.py`, extracted 2026-08-12 from `voice_twiml.py` so the TwiML webhook and the SMS webhooks below share one implementation) validates `X-Twilio-Signature` against `TWILIO_AUTH_TOKEN` — this is a real no-op (always accepts) if `TWILIO_AUTH_TOKEN` is unset, which is the case until the Twilio account is funded and configured.

### SMS sending (Twilio Messaging API)

**Purpose:** marketing SMS sends. Added 2026-08-12, Phase 1 of a larger
messaging design (`docs/messaging-design.md`; see
[Architecture → SMS marketing send](architecture.md#sms-marketing-send-phase-1-of-messaging)
and `AGENTS.md` §2026-08-12). The shop's own Twilio DID — the same number
that answers voice calls — is also the SMS sender; no second number, no
shared Kairo sender.

**Key files:** `booking_engine/services/messaging/{sms_send,gsm7,send_credits}.py`, `booking_engine/db/sms_queries.py`, `booking_engine/api/routes/sms.py`, `booking_engine/services/twilio_signature.py`.

**Env vars:** `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` (the Twilio send) plus `WEBAPP_BASE_URL`/`MARKET_INTEL_SECRET` (the post-send charge — `sms_send.py` bills the shop by POSTing to the webapp's charge-actual endpoint via `booking_engine/clients/webapp_credits.py`; the webapp owns the basket deduction). `sms_send.py` calls `twilio.rest.Client.messages.create()` directly; it does not go through `clients/twilio_numbers.py`, which is provisioning-only (search/purchase numbers).

**No in-message opt-out (STOP handling removed).** There is no inbound SMS
webhook, no STOP-keyword parsing, and no opt-out footer — see `AGENTS.md`'s
STOP-removal entry. Suppression is `business_app_core.customers.
marketing_consent` alone, cleared in-store by a staff member; `sms.opt_outs`
still exists in the schema but nothing writes to it any more. Provisioned
numbers no longer set an `sms_url` webhook (`clients/twilio_numbers.py::purchase_number`),
and `services/number_health.py::decide_health` checks `voice_url` only.

**Gotcha — segment/encoding is a billing surface, not cosmetic.** GSM-7 fits 160 chars/segment; the moment a body contains one character GSM-7 can't represent (most emoji, curly quotes, em dashes, uppercase accented vowels other than É), the whole message drops to UCS-2 at 70 chars/segment — silently tripling the bill on a stray LLM-written curly quote. `gsm7.py`'s `sanitize()` transliterates that typographic noise back into GSM-7 losslessly; genuinely non-GSM-7 content (emoji) is priced honestly, never silently stripped. Segment counting is duplicated in the webapp (`src/lib/messaging/sms-preview.ts`, a pre-click cost preview) rather than shared — this repo's count is authoritative at send time.

**Billing: 2× Twilio cost via AI credits, a dedicated converter.** `send_credits.py` — deliberately not the webapp's `rawToUserCredits()` (10× LLM margin, floors at 1 credit). Full reasoning: `AGENTS.md` §2026-08-12.

### WhatsApp (Meta Cloud API, Tech Provider)

**Purpose:** personalised marketing to consenting customers, ~50/day/salon,
plus bulk campaigns dripped over days. Added 2026-08-21 on Twilio; **migrated
off Twilio onto Meta direct 2026-08-24**. See
[Architecture → WhatsApp marketing](architecture.md#whatsapp-marketing-one-waba-per-salon)
and `AGENTS.md` §2026-08-24.

> **Twilio is not in this path.** Twilio must attach a WABA to *its own* Meta
> credit line during registration and Meta only lets a payment method be
> revoked, never removed — so a WABA created outside Twilio fails with
> [63103](https://www.twilio.com/docs/api/errors/63103). Twilio's own docs say
> *"Don't select a WABA that's been created outside of Twilio"*, and its
> migration path warns *"You won't be able to continue using WhatsApp or
> WhatsApp Business App with the same phone number."* Hairdressers run their
> business from that app. Twilio keeps voice and SMS.

**Key files:** `booking_engine/clients/meta_whatsapp.py` (Graph client),
`booking_engine/services/meta_signature.py`,
`booking_engine/services/messaging/{whatsapp_onboarding,whatsapp_send,whatsapp_templates,whatsapp_pricing,pacer,meta_limits}.py`,
`booking_engine/db/whatsapp_queries.py`, `booking_engine/api/routes/whatsapp.py`,
`scripts/kairo_waba.py` (drives Kairo's *own* WABA — App Review evidence and
template validation).

**Coexistence is the feature.** Meta's Embedded Signup (May 2025) can connect a
number that is already live on the WhatsApp Business App to Cloud API, keeping
both active: the salon keeps chatting from their phone while Kairo sends
templates through the API. Enabled with
`featureType: "whatsapp_business_app_onboarding"` in the popup config — no
allowlist request, but it does require Advanced access to
`whatsapp_business_management`. Constraints: fixed 20 msg/s throughput, one
number per coexistence account, not available for NG/ZA numbers, and the
onboarding must be finished within 24h of the popup or the salon starts over.

**What a message costs (`whatsapp_pricing.py`, Italy, 2026-08):** Meta's rate,
and nothing else. Going direct removed Twilio's flat per-message platform fee.

| Kind | Meta | Who is billed |
|---|---|---|
| Marketing template | $0.0691 | the salon, by Meta, directly |
| Utility template (reminders) | $0.0341 | " |
| Free-form inside the 24h window | $0 | — genuinely free now |

That last row restores what `docs/messaging-design.md` §5.1 originally claimed
and the 2026-08-22 entry corrected: under Twilio it cost the platform fee;
under Meta direct it does not.

**Billing: Kairo debits nothing for a WhatsApp send.** A Tech Provider (unlike
a Solution Partner) has no credit line to share, so each salon attaches its own
card to its own WABA. `try_debit_for_message` is gone from this path;
`price_usd` is a send-time estimate that is never corrected, because Meta
reports no amount on send or on the webhook. The plan allowance
(`subscription_plans.whatsapp_monthly_messages`) still applies — it is a
product limit, not cost recovery. **SMS is unchanged in amount and still
charges 2×** — the deduction now happens via an HTTP charge to the webapp's
basket (`webapp_credits.py`), not a local write.

**The one thing Kairo *does* charge on the WhatsApp path is AI work on inbound
messages**, which is our cost and not Meta's: triage
(`clients/marketing_triage.py`, billed by the marketing-engine gateway),
voice-note transcription (see [OpenAI audio transcription](#openai-audio-transcription)),
and each booking-agent turn (`clients/marketing_agent.py`). All three refuse on
an empty basket rather than run unpaid.

**The booking agent turn — `POST {MARKET_INTEL_API_URL}/whatsapp/agent`, bearer
`MARKET_INTEL_SECRET`.** The turn itself (prompt, model, tool-calling loop)
lives in marketing-engine; `clients/marketing_agent.py` is the thin client.
Request carries `{shop_id, call_id, shop_name, services[], intake{}, messages[],
first_turn, customer_name, customer_phone, now}`; a 200 answers
`{data: {text, escalate, reason, tool_calls}, llm_cost_usd}`.

- **The gateway gates and charges this turn, not this repo.** It reads the
  shop's basket before calling a provider (**402** when empty, with
  `reason: 'no_credit'`) and settles the *actual* LLM cost against that same
  basket after a turn that ran. So there is exactly one debit path for a turn.
  A `webapp_credits.charge_actual` call on this side would stack an invented
  flat charge on top of a real one — the double-debit the 2026-08-12 decision
  forbids and the 2026-09-03 one deleted this repo's basket arithmetic to
  prevent. What this repo owns instead is the ceiling the gateway cannot see:
  `wa_agent.MAX_SESSION_TURNS` (12), counted per session.
- **`call_id` is not bookkeeping.** The gateway passes it back to *this* repo's
  voice tools, which read the shop off the session row and never off a header —
  it is the authorization basis for every booking the turn touches.
- **`text` is empty whenever `escalate` is true**, by contract. An agent that
  apologises in a way that still reads like an answer leaves the customer
  waiting for a reply that is not coming. `marketing_agent` re-blanks it anyway,
  so a future change on the far side cannot answer the customer *and* hand the
  thread to the owner.
- **Every failure is an escalation with a reason** — unconfigured, 402, engine
  down, timeout, malformed JSON — never a guess at what the agent would have
  said. 45s timeout (longer than triage's 15s: a turn may run a tool loop).

**The Login Configuration behind `META_CONFIG_ID` is where two facts live that
no code in this repo can see.** Its permission list must be exactly
`whatsapp_business_management` + `whatsapp_business_messaging` (asset: WhatsApp
accounts) — anything else needs its own App Review or the salon is never
granted it. And its **token expiry** decides whether every connected sender
dies on a timer: Kairo's config is the 60-day variant, so
`senders.token_expires_at` is populated and nothing renews it. The config is
not readable through Graph (`GET /{config_id}` → code 100, subcode 33) — check
it by eye in *Accesso con Facebook per le aziende → Configurazioni*.

**Env vars:** `META_APP_ID`, `META_CONFIG_ID`, `META_SOLUTION_ID` (public,
served to the webapp so Embedded Signup has one source of truth. **The
solution id is normally empty and that is correct** — a *partner solution* is
a joint arrangement with a Solution Partner/BSP, created against their Partner
App ID and `Pending` until they accept it, and Kairo onboards
[independently](https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers):
each salon attaches its own payment method, so there is no solution to name.
The webapp passes it as `extras.setup.solutionID` when set and omits the field
when not — a blank string is rejected. Do not paste `META_CONFIG_ID` here;
they are different objects and nothing validates the difference),
`META_APP_SECRET` (verifies `X-Hub-Signature-256` *and* signs the code
exchange), `META_VERIFY_TOKEN` (webhook handshake),
`META_KAIRO_WABA_ID`/`META_KAIRO_TOKEN` (Kairo's own WABA — the template
approval gate reads these; unset means `ensure_templates` propagates nothing),
`WHATSAPP_SEND_START_HOUR`/`WHATSAPP_SEND_END_HOUR` (default 9/20,
Europe/Rome), `WHATSAPP_SENDS_PER_MINUTE` (default 60),
`WHATSAPP_TOKEN_KEY` (Fernet key encrypting `senders.access_token` at rest —
see below), and `WEBAPP_BASE_URL`/`MARKET_INTEL_SECRET`, shared with the SMS
charge path, which the tick also uses to ask the webapp to email a salon whose
token is about to expire.

**`access_token` is encrypted at rest (2026-09-20).** It is full authority over
one salon's WhatsApp with no shared parent credential behind it to revoke —
plaintext from 2026-08-24 until this. Sealed and opened in
`db/whatsapp_queries.py`, at the one boundary every caller already crosses, so
no service or route knows about it; the cipher is Fernet
(`services/secret_box.py`) and the key lives in Fly secrets rather than in the
database it protects. A sealed value carries a `v1:` prefix, which is what let
this land with no migration and no backfill window: a row written before the
key existed still reads, and is re-sealed the next time anything writes it.
**Unset keeps the old behaviour** and logs loudly at every write — refusing
would take WhatsApp offline to fix a threat that is about a database dump.
Rotating the key requires re-sealing every row first; there is no dual-key
read path.

**The Graph calls, in the order onboarding makes them** (all `v26.0`, pinned —
Graph changes shape across versions):

| What | Endpoint |
|---|---|
| Exchange the popup's code for the salon's token | `GET /oauth/access_token?client_id&client_secret&code` |
| Subscribe our app to their WABA | `POST /{waba_id}/subscribed_apps` |
| Confirm coexistence | `GET /{phone_number_id}?fields=platform_type,is_on_biz_app` |
| Check Kairo's own copy is approved (gate, before injecting) | `GET /{kairo_waba_id}/message_templates?name=…` |
| Inject a template | `POST /{waba_id}/message_templates` |
| Template verdict (reconciler) | `GET /{waba_id}/message_templates?name=…` |
| Send | `POST /{phone_number_id}/messages` `{type: "template", template: {...}}` |

**The constraints that decide the product, not just the code:**

- **Marketing = approved template only.** No free-form business-initiated
  message exists outside Meta's 24-hour customer-initiated session window.
  Personalisation is variable substitution inside an approved skeleton.
- **A template that is essentially one big `{{1}}` is the most common cause
  of outright Meta rejection**, and a rejected template sends nothing at all —
  hence the fixed scaffolding in `whatsapp_templates.py`.
- **Variable values must be single-line.** Meta rejects parameters containing
  newlines, tabs, or 4+ consecutive spaces; `clean_variable()` enforces that
  locally rather than letting the send fail at the provider.
- **Templates are per-WABA, so per-salon**, and must be *created* in each
  salon's WABA — the call Twilio structurally could not make. Meta blocks
  reusing a deleted template's name for 30 days, so `ensure_templates` skips
  rather than recreates.
- **One template per locale, named `{locale}_{key}`** (`it_promo_v1`). Meta
  cannot translate a template, so each language is a separate submission with
  its own verdict; the shop's `shops.language` composes the name. Italian is
  the only copy that exists today — see
  [Templates carry the locale](api/whatsapp.md#post-whatsapptemplatesensureshop_id).
- **A rejection is content, not per-WABA luck.** `ensure_templates` only
  pushes a catalogue entry into a salon's WABA once the same-named template is
  `approved` on Kairo's own WABA (`scripts/kairo_waba.py push-templates`,
  reviewed by hand). One rejection there is cheaper than N of them, and avoids
  the quality-rating hit of rejecting on every salon's WABA independently.
- **Messaging limit tiers.** An unverified WABA is capped at 250
  business-initiated conversations per 24h — 50/day/salon sits well inside it,
  which is why Meta Business Verification is *not* a prerequisite for a
  *salon* (it is for Kairo, to pass App Review at all).
- **Marketing to US recipients is dead** (Meta, since 2025-04-01). Irrelevant
  for Italian salons; relevant the day anyone tries this elsewhere.
- **Every ceiling above is enforced in `meta_limits.py`, and fails closed.**
  `effective_daily_cap()` is `min(Meta's tier, our `daily_cap`)`, so a
  commercial knob can only narrow the platform limit, never widen it; an
  unrecognised tier is treated as the unverified 250. The tier check uses a
  **rolling 24h** count, not the calendar day — the two differ by nearly a
  full tier around midnight.
- **`131049` is not `131050`.** `131049` is Meta's per-user *cross-brand*
  marketing cap — the recipient has had enough marketing today, from anyone —
  and must be retried after 24h, not treated as an opt-out. `131050` is the
  real opt-out and is permanent. Conflating them silences customers who did
  nothing wrong.
- **Templates can be paused for negative feedback**: 3h, then 6h, then
  permanently deactivated. Sends on a paused template fail.
- **Tech Provider onboarding limit:** 10 new customers per rolling 7 days,
  raised to 200 by completing Access Verification.

**Two Meta-side surprises worth knowing before debugging:**

- **Webhook signatures cover the raw body, not the parsed JSON.**
  Re-serialising changes whitespace and key order and every genuine request
  starts failing. One app secret, all tenants — the opposite of Twilio's
  per-account signing.
- **Forgetting `subscribed_apps` fails silently in the worst direction.**
  Sends keep succeeding; you just never hear about delivery, template
  verdicts or opt-outs. Hence its position before everything else in
  `complete()`.

**Manual, out-of-band prerequisites (not automatable from this repo):** create
a Meta app (type Business), complete 2FA + Business Verification on Kairo's
Meta Business Portfolio, stand up Kairo's own WABA (`scripts/kairo_waba.py`),
pass **App Review** for Advanced access to `whatsapp_business_management` +
`whatsapp_business_messaging` (two screen-recordings of the *business-facing*
UI: one sending a message, one creating a template), then complete Tech
Provider onboarding ("Onboard without a partner") for the Solution ID and
Access Verification. **Embedded Signup v2 is deprecated 2026-10-15 — build on
v4.** See Meta's
[Become a Tech Provider](https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers)
and [Onboard WhatsApp Business app users](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/).

## OpenAI Realtime

**Purpose:** speech-to-text, LLM reasoning, text-to-speech, and voice activity detection for the live call — via **native SIP** (production path) or an ephemeral browser/WebRTC session (local testing only).

**Key files:** `booking_engine/clients/openai_realtime.py` (`accept_sip_call`, `create_ephemeral_session`), `booking_engine/api/routes/voice_openai.py` (`realtime.call.incoming` webhook), `booking_engine/services/call_supervisor.py`, `booking_engine/mcp_server.py` (hosted MCP tool mount).

**Env vars:** `OPENAI_SIP_PROJECT_ID`, `OPENAI_API_KEY`, `OPENAI_REALTIME_MODEL` (`gpt-realtime` — not `gpt-4o-realtime-preview`), `OPENAI_WEBHOOK_SECRET`, `VOICE_AGENT_TOOL_SECRET`, `ENABLE_CALL_SUPERVISOR`, `CALL_SUPERVISOR_VERBOSE_LOGGING`.

**Hard-won gotcha #1 — hosted MCP does not auto-continue.** After a tool call, the model's response ends (`response.done` fires *before* the tool even returns); the tool executes, `response.output_item.done` delivers the result, and then nothing — OpenAI does not open a new response to voice it. This directly contradicts the Responses-API "hosted MCP auto-continues" assumption. Full event-trace evidence and the fix (a server-side control WebSocket sending `response.create`) in `AGENTS.md` §2026-07-21 (two entries: "Realtime + hosted MCP..." and "SIP call supervisor...").

**Hard-won gotcha #2 — `server_url` needs a trailing slash.** `app.mount("/mcp", ...)` makes Starlette 307-redirect bare `/mcp` → `/mcp/`, and OpenAI's Realtime MCP client does **not** follow that redirect for the tool-call POST body — it silently never calls the tool. Always point `server_url` at `/mcp/`. Root-caused via `fly logs`; full story in `AGENTS.md` §2026-07-21 "MCP server_url must carry a trailing slash".

**Gotcha #3 — webhook signature is opt-in.** `voice_openai.py`'s `realtime.call.incoming` handler only verifies a signature when `OPENAI_WEBHOOK_SECRET` is set (see the `ponytail:` comment at the top of that file) — currently unwired, so the endpoint accepts unsigned requests.

## OpenAI audio transcription

**Purpose:** transcribing inbound **WhatsApp voice notes**. On this vertical that is not an edge case — customers send "vorrei fare il colore come l'altra volta" as audio far more often than they type it, and an untranscribed note arrives with an empty `body` and `message_type = 'audio'`: unreadable in the Inbox, invisible to the router.

**Key files:** `booking_engine/services/messaging/wa_transcribe.py`, `booking_engine/clients/meta_whatsapp.py::get_media` (the two-hop download), `booking_engine/clients/webapp_credits.py` (the charge).

**Endpoint / model:** `POST https://api.openai.com/v1/audio/transcriptions`, model `gpt-4o-mini-transcribe`, `response_format=text`. Plain `httpx` with the same `OPENAI_API_KEY` the Realtime path uses — no `openai` SDK dependency, matching `clients/openai_realtime.py`. Meta delivers voice notes as `audio/ogg` (opus) and the upload's **filename extension** is how the endpoint decides how to decode it, so the multipart part is named `voice.ogg`. No `language` hint is sent — forcing `it` would mangle a non-Italian customer rather than merely detect them less reliably.

**Env vars:** `OPENAI_API_KEY` (shared with Realtime) plus `WEBAPP_BASE_URL`/`MARKET_INTEL_SECRET` for the charge. No key → fails closed *without* charging.

**Why here and not in the LLM gateway.** The audio bytes need the salon's business token to come off Meta, and this repo is where that token lives (encrypted at rest, `services/secret_box.py`). Transcribing in marketing-engine would mean moving a secret in order to move a payload.

**AI spend: 15 credits per voice note, flat, run type `whatsapp_transcribe`.** Derived from real cost at the ceiling of a typical note rather than rounded: 30s = 0.5 min × $0.003/min = $0.0015 raw, × 10 (the house LLM margin — this is LLM spend, not the 2× carrier pass-through `send_credits` applies to Twilio) × 1000 credits/USD = 15. Flat rather than per-second because a duration meter would mean decoding the container to learn something worth a fraction of a credit; a note longer than 30s is therefore transcribed under cost, bounded only by Meta's 16MB media cap.

**Charged *before* the work — the opposite of the SMS path, deliberately.** SMS debits only after Twilio accepts (`AGENTS.md` §2026-08-12). Here the charge is the gate: a 402 means an empty basket, so nothing is sent to OpenAI at all and the raw message stays in the owner's queue as a vocale. The cost of that ordering is that a provider failure leaves the salon charged for nothing — accepted rather than refunded, because the only refund available would be a negative `charge-actual`, which is basket arithmetic this repo gave up in §2026-09-03 and a contract the webapp does not offer. It is logged at `whatsapp.transcribe_failed` with the credit amount so the exposure is countable.

**Every failure returns `None`, never a partial transcript** — no credit, no key, media gone, provider error, empty result. `None` is stored as a NULL `transcript` ("we do not know"); `""` would claim we transcribed it and it said nothing. It never overwrites `body`. `transcribe()` cannot raise: it is awaited from a fire-and-forget background task where an exception is a silently lost customer message.

## Neon PostgreSQL

**Purpose:** the shared database — `business_app_core` (owned by the `webapp` Control Plane) plus `voice_agent` (owned by this repo). See [Database](database.md).

**Key files:** `booking_engine/db/connection.py` (asyncpg pool).

**Env vars:** `DATABASE_URL` (pooler endpoint, port 5432, transaction mode).

**CI usage:** every DB-touching GitHub Actions workflow (`ci.yml`, `deploy-qa.yml`, `deploy-fly-prod.yml`) provisions a throwaway, copy-on-write Neon branch off production, migrates + tests against it, then deletes it — never touches the real QA/production branch until that passes. Full rationale (a real seed-data bug this caught) in `AGENTS.md` §2026-07-18.

**Migration ownership:** the ephemeral branch above is the only Neon branch this repo migrates itself. Applying migrations to the real QA/production branches belongs to the `webapp` repo, which orders all three schemas together — see [Operations → Migrations](operations.md#migrations) and `AGENTS.md` §2026-07-24 "CI: migration ownership moved to the webapp repo".

## Push notifications (stub, not wired)

**Purpose:** intended to alert merchants (low balance, new callback memo, etc.) on their devices.

**Key files:** `booking_engine/clients/push_notifications.py`.

**Current state:** `send_push()` only logs the event — "Plan C wires this to the webapp's existing notification infrastructure" is a comment, not yet built. Don't assume any push notification actually reaches a merchant device today.
