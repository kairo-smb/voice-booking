# Architecture

> **Maintenance rule:** a change to the service topology, the call flow, or the tool-dispatch mechanism updates this file in the same change. See [README](README.md#maintenance-rule).

---

## Topology

```
Caller (phone) → Twilio (TwiML) → OpenAI Realtime API (native SIP, STT/LLM/TTS)
                                          ↕ MCP tool calls (in-process ASGI, /mcp)
                                    booking_engine (Fly.io, single service)
                                          ↕ SQL (asyncpg)
                                    Neon PostgreSQL (business_app_core + voice_agent)
```

One deployed service — `booking_engine`, a FastAPI app (`booking_engine/api/app.py`). There is no separate "voice gateway" process; an earlier two-service split was folded into this one before the current history log begins — the only surviving trace is the `tests/voice_gateway/` directory name, which today tests `booking_engine/services/*` (`CLAUDE.md` §2026-07-24 "Repo cleanup...", which deleted the last dead references to it).

## Call flow

1. A call reaches a Twilio number. Twilio POSTs to `POST /api/v1/voice/twiml/incoming` (`voice_twiml.py`), signature-checked against `TWILIO_AUTH_TOKEN`. The handler looks up the shop by the dialed number and returns TwiML that `<Dial><Sip>`s straight into OpenAI's SIP gateway, passing the shop id as a custom SIP header (`X-Shop-Id`, via Twilio's `<Dial><Sip>` query-string-after-host convention — see `services/realtime_session.py::build_sip_uri`).
2. OpenAI fires `realtime.call.incoming` to `POST /voice/openai/incoming` (`voice_openai.py`, top-level path, not under `/api/v1`). The handler reads `X-Shop-Id` back out of the SIP headers (or, QA-only, falls back to `SIP_TEST_FALLBACK_SHOP_ID` for a raw softphone test call with no Twilio in the path), resolves the caller by phone (`services/identity_resolver.py`), assembles the session prompt (`services/prompt_assembler.py` — see [Voice Agent Logic](voice-agent-logic.md)), and calls `accept_sip_call()` (`clients/openai_realtime.py`) with that prompt + the 12 tool schemas.
3. During the call, OpenAI calls tools over MCP against `/mcp` (mounted directly on this app in `app.py`, via `booking_engine/mcp_server.py`). Tool dispatch is **in-process** — `execute_tool()` uses an `ASGITransport(app=app)` call into the exact same running process rather than a real HTTP hop, wrapped in a 10s `asyncio.wait_for` (`TOOL_CALL_TIMEOUT_SECONDS`, `services/mcp_tools.py`) that returns a clean `{"ok": false, "error": "tool_timeout"}` on a stuck downstream call rather than hanging. This was a deliberate fix for real "dead air" latency caused by an earlier version that made a genuine outbound HTTPS request to the app's own public URL on every tool call — full incident in `CLAUDE.md` §2026-07-24.
4. If `ENABLE_CALL_SUPERVISOR` is set, a per-call background task (`services/call_supervisor.py`) opens its own control WebSocket to the accepted call and sends `response.create` on connect (greeting) and after each tool result (`response.output_item.done` for an `mcp_call`) — working around OpenAI's hosted MCP not auto-continuing after a tool result on its own. Off by default; see `CLAUDE.md` §2026-07-21 for why it exists and its current live-test status.
5. On hangup, the call is finalized via `voice_events.py`'s `session.*` webhooks (started/turn/ended), persisting to `voice_agent.calls`/`call_transcripts`/`call_events`.

## Auth boundaries

Four distinct auth schemes across the surface — see [API overview](api/README.md) for the full breakdown:
- `CONTROL_PLANE_SECRET` — the separate `webapp` Control Plane repo, reading/writing voice config, calls, analytics, telephony provisioning, and (since 2026-08-12) triggering a one-off SMS send (`POST /api/v1/sms/send`).
- `OPENAI_TOOL_SECRET` — OpenAI's Realtime tool/event calls (`/voice/tools/*`, `/voice/events/*`), and the MCP mount.
- Twilio request-signature verification (`TWILIO_AUTH_TOKEN`) — the TwiML webhook and, since 2026-08-12, the two SMS webhooks (`services/twilio_signature.py`, one verifier shared by all three routes).
- The OpenAI `realtime.call.incoming` webhook (`/voice/openai/incoming`) currently verifies a signature **only if `OPENAI_WEBHOOK_SECRET` is set** — unset, it accepts unsigned requests (a known, flagged gap; see the `ponytail:` comment at the top of `voice_openai.py`).
- The plain REST API (`shops`, `customers`, `services`, `availability`, `appointments`) has **no auth dependency at all** today.

## Self-service number provisioning (Path 2 onboarding)

A second, independent flow alongside the SIP call path above — no caller involved, just the webapp and Twilio's Regulatory Compliance API:

1. Webapp's "Richiedi numero" panel (Inbox → Configurazione → Canali) POSTs `business_name`/`contact_email`/a commercial-register document to `POST /api/v1/voice/numbers/request` (`voice_telephony.py`, control-plane bearer). `services/number_provisioning.py::submit_request` builds **that salon's own** Twilio regulatory bundle — regulation lookup → End-User → document upload → Bundle → 2× ItemAssignment → synchronous Evaluate → submit-if-compliant — persisting each Twilio SID to `voice_agent.number_requests` as soon as it exists, not batched at the end.
2. `POST /api/v1/messaging/tick` (`messaging_tick.py`), hit hourly by `.github/workflows/messaging-cron.yml`, polls every `pending_review` request's bundle status, calls `services/number_provisioning.py::provision_approved` to purchase the number once Twilio approves, refreshes the green/red health semaphore (`services/number_health.py`) for every already-provisioned number, then — last, and wrapped in its own try/except so its failure can't suppress the health refresh above it — runs `services/number_release.py::sweep`, the grace-period release of numbers whose shop's plan has lapsed (schedule → clear-if-plan-returns → release-past-deadline; see `api/number-provisioning.md` and `CLAUDE.md` §2026-08-15).
3. `GET /api/v1/voice/numbers/request/{shop_id}` is the webapp's poll target — returns the request row and the telephony row (if any) so the UI can pick which state to render.
4. `POST /api/v1/voice/numbers/release` (`voice_telephony.py`) is the owner-initiated counterpart to the sweep above — a salon deliberately giving up its number, bypassing the grace period on purpose. Calls the same `release_for_shop` the sweep uses.

**This coexists with, and does not replace, the older manual `/voice/numbers/search` + `/voice/numbers/provision` pair** — those still purchase against the one shared Kairo-entity bundle (`TWILIO_BUNDLE_SID`) from the 2026-07-16 decision, used for Path 1 (forwarding) and ops-triggered onboarding. Only the new `/request` path builds a bundle per salon. Full rationale for why a shared bundle is no longer viable for self-service: `CLAUDE.md` §2026-08-14. Design detail (regulatory model, the orphaned-number bug and its fix, the health-semaphore contract): see the git history of the now-deleted `docs/number-provisioning-design.md`, or `CLAUDE.md` §2026-08-14.
## SMS marketing send (Phase 1 of messaging)

First shipped piece of a larger SMS/WhatsApp messaging design
(`docs/messaging-design.md` — a working document, deleted once the whole
design ships; the durable record is this section, [Database → `sms`
schema](database.md#sms-schema--authoritative-here), [Providers →
SMS](providers.md#sms-sending-twilio-messaging-api), [API →
SMS](api/sms.md), and `CLAUDE.md` §2026-08-12). Phase 1 is one outbound SMS
to one consenting customer, triggered from the webapp:

```
webapp (owner clicks "Invia SMS" in a modal)
  → POST /api/v1/hair-salon/customers/{id}/send-sms   (webapp's own route; re-checks consent)
    → POST /api/v1/sms/send                            (this repo, CONTROL_PLANE_SECRET)
        services/messaging/sms_send.py::send_marketing_sms
          consent gate (marketing_consent alone — no in-message opt-out) → sanitize (gsm7.py)
          → balance check → Twilio send (with status_callback) → debit credits (token_basket_queries)
          → persist to sms.outbound_messages
```

**No inbound SMS webhook / STOP handling.** Opt-out was removed as an
in-message mechanism — see `CLAUDE.md`'s STOP-removal entry. Suppression is
`business_app_core.customers.marketing_consent` alone, cleared in-store by
staff. Twilio still POSTs delivery/price callbacks to
`POST /api/v1/sms/webhook/status`, `X-Twilio-Signature`-verified with the
same helper the TwiML webhook uses.

**Single debit path, by design.** Only `sms_send.py` ever calls
`try_debit_for_message` — the webapp's `send-sms` route forwards the
request and re-checks consent but never touches credits itself, so there
is exactly one place a send can be billed.

**Synchronous, not queued.** `/sms/send` blocks until Twilio accepts the
message or the send is refused, because the caller is a salon owner
watching a modal, not a batch job. Bulk/scheduled sends (`sms.campaigns`)
are a later phase — nothing writes that table yet.

## WhatsApp marketing (one WABA per salon)

Personalised promotions over WhatsApp instead of SMS, ~50/day/salon dripped
across opening hours. Durable record: this section, [Database → `whatsapp`
schema](database.md#whatsapp-schema--authoritative-here), [Providers →
WhatsApp](providers.md#whatsapp-meta-tech-provider--twilio), [API →
WhatsApp](api/whatsapp.md), and `CLAUDE.md` §2026-08-21.

**The shape is forced by two hard external rules, not chosen:**

1. **A business-initiated marketing message can only be a template Meta
   approved in advance.** There is no free-form path. The end-to-end LLM copy
   the SMS flow sends cannot be sent on WhatsApp as-is — personalisation
   happens *inside variables*, in a fixed approved skeleton
   (`services/messaging/whatsapp_templates.py`).
2. **A WABA can only be created or connected by the salon**, inside Meta's
   browser popup. There is no server-side API that does it on a customer's
   behalf.

Kairo is a Meta **Tech Provider** talking to `graph.facebook.com` directly.
There is no BSP: Twilio cannot register a WABA it did not create
(error 63103) and its migration path deletes the salon's WhatsApp Business
App — a non-starter for a hairdresser who runs the business from that app.

```
Kairo = Meta Tech Provider (one Meta app + Embedded Signup, in the webapp)
  └── per salon: the salon's OWN WABA, owned and paid for by the salon
                                   ──> 1 business phone number (Cloud API)
                                   ──> Kairo's templates, injected by us
```

**Coexistence is the point.** The salon's existing WhatsApp Business App
number stays live on their phone — they keep chatting with clients from the
app — while the same number also sends templates through Cloud API. Enabled
with `featureType: "whatsapp_business_app_onboarding"` in the popup config.

Onboarding is **one round trip**; the popup performs verification itself, so
there is no OTP to relay and no inbound-SMS webhook:

```
POST /whatsapp/onboarding/start     → record intent, return Embedded Signup config
   (salon completes Meta's popup; the browser gets code + waba_id + phone_number_id)
POST /whatsapp/onboarding/complete  → exchange code for the salon's business token
                                    → subscribe our app to their WABA  (before anything else)
                                    → register the number (source='new' only)
                                    → confirm coexistence from Meta, not from the popup
                                    → inject the template catalogue
```

**Nothing here debits AI credits.** A Tech Provider has no Meta credit line to
share, so the salon's own card sits on the salon's own WABA and Meta charges
it directly. Debiting on top would bill one message twice. The plan allowance
still caps volume; the SMS path is unchanged and still debits 2×.

**Sending is queued, not synchronous** — the opposite of `/sms/send`, and for
the reason that motivates the feature:

```
webapp (LLM writes the per-customer offer line, i.e. the template's {{3}})
  → POST /api/v1/whatsapp/campaigns          (CONTROL_PLANE_SECRET)
      whatsapp_send.py::enqueue_campaign
        sender online? template approved? recipients ≤ daily_cap?
        → per recipient: consent gate → one row in whatsapp.outbound_messages,
          scheduled_at spread evenly across 09:00–20:00 Europe/Rome
  → POST /api/v1/messaging/tick              (hourly cron)
      whatsapp_send.py::send_due
        claim due rows atomically → RE-check consent → balance check
        → Meta Cloud API send (paced) → mark sent
```

**Consent is re-read at send time, not trusted from enqueue.** A queued row
can sit for hours; a customer whose consent is cleared in-store at 11:00 must
not receive the message scheduled for 15:00. Same trust-boundary reasoning as
`sms_send.py`'s re-check, but it matters more here because of the delay.

**WhatsApp restores the self-service opt-out SMS gave up.** Meta puts a
native "Stop promotions" button on every marketing template; a refusal comes
back as error **`131050`** on the status webhook, which writes
`marketing_consent = false` into `business_app_core.customers`. That is a
materially stronger position under Italian marketing rules than the SMS path
(see `CLAUDE.md`'s STOP-removal entry), and it comes from Meta, not from us.

**`131049` is a different thing and must not be conflated with it.** That is
Meta's per-user *cross-brand* marketing cap — the recipient has had enough
marketing today, from anyone — so the message is requeued 24h later, not
suppressed. Treating it as an opt-out permanently silences customers who did
nothing wrong.

**Bulk campaigns drip across days, not hours.** `spread()` chunks a campaign
by the sender's `daily_cap` and rolls onto following days, so a 400-recipient
send against a 50/day sender is an eight-day schedule the owner is shown at
enqueue time — rather than 350 rows the tick defers by an hour, over and over.
Within a tick, sends are paced (`services/messaging/pacer.py`) against the
Graph API's app-level limit, which every tenant shares.

## Alternate entrypoint (testing only)

`clients/openai_realtime.py::create_ephemeral_session` mints a browser/WebRTC session for local testing (`scripts/voice_test_server.py`, `scripts/run_webrtc_harness.sh`) — a different transport than production SIP calls. See [Operations](operations.md) for the live-SIP softphone test path, which exercises the real call flow above end-to-end without needing a funded Twilio number.
