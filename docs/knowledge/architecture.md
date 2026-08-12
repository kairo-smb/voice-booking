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
          consent + opt-out gate → sanitize + append footer (gsm7.py)
          → balance check → Twilio send → debit credits (token_basket_queries)
          → persist to sms.outbound_messages
```

Inbound: Twilio POSTs STOP replies to `POST /api/v1/sms/webhook/inbound`
and delivery/price callbacks to `POST /api/v1/sms/webhook/status`, both
`X-Twilio-Signature`-verified with the same helper the TwiML webhook uses.

**Single debit path, by design.** Only `sms_send.py` ever calls
`try_debit_for_message` — the webapp's `send-sms` route forwards the
request and re-checks consent but never touches credits itself, so there
is exactly one place a send can be billed.

**Synchronous, not queued.** `/sms/send` blocks until Twilio accepts the
message or the send is refused, because the caller is a salon owner
watching a modal, not a batch job. Bulk/scheduled sends (`sms.campaigns`)
are a later phase — nothing writes that table yet.

## Alternate entrypoint (testing only)

`clients/openai_realtime.py::create_ephemeral_session` mints a browser/WebRTC session for local testing (`scripts/voice_test_server.py`, `scripts/run_webrtc_harness.sh`) — a different transport than production SIP calls. See [Operations](operations.md) for the live-SIP softphone test path, which exercises the real call flow above end-to-end without needing a funded Twilio number.
